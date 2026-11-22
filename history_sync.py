"""Read the selected Telegram forum topic through a *user* Telethon session.

Commands: login, topics, sync. Messages are kept for 30 days as before.
"""
import asyncio
import os
import sys
import json
from telethon_diagnostics import DiagnosticError, classify, result
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, utils
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions import messages as tl_messages, channels as tl_channels

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
SESSION = str(ROOT / 'user_history_session')


def session_source():
    value = os.getenv('TELETHON_STRING_SESSION', '').strip()
    return StringSession(value) if value else SESSION

def require_session():
    if not os.getenv('TELETHON_STRING_SESSION', '').strip() and not Path(SESSION + '.session').exists():
        raise ValueError('Нет сессии Telethon. Укажи TELETHON_STRING_SESSION или выполни локальный вход.')



def use_shared_telegram():
    return os.getenv('BOT_MANAGER_TELEGRAM_SHARED') == '1'


def shared_request(endpoint, payload):
    """Talk only to the short-lived launcher-local gateway, never open its session."""
    import requests
    from urllib.parse import urlparse
    url = os.getenv('BOT_MANAGER_TELEGRAM_URL', '').strip()
    token = os.getenv('BOT_MANAGER_TELEGRAM_TOKEN', '').strip()
    parsed=urlparse(url)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or not token:
        raise ValueError('Общий Telegram не настроен. Запусти BOT Point из Bot Manager.')
    try:
        response=requests.post(url.rstrip('/') + endpoint, json=payload,
            headers={'X-Bot-Manager-Token':token}, timeout=235)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise RuntimeError('Нет связи с общим Telegram лаунчера. Проверь авторизацию и запусти бота из Bot Manager.') from exc


def sync_shared():
    chat_id, topic_id=target_settings()
    from app import connect, save_report, init_db, should_validate_id
    init_db()
    result=shared_request('/v1/messages', {'chat_id':chat_id,'topic_id':topic_id})
    cutoff=datetime.now(timezone.utc)-timedelta(days=30)
    scanned=matched=review=0
    for msg in result['messages']:
        date=int(msg['date_utc'])
        if date<int(cutoff.timestamp()):continue
        scanned+=1
        with connect() as db:
            report=save_report(db,chat_id,int(msg['id']),topic_id,date,msg.get('text',''),
                               enforce_missing_id=should_validate_id(int(msg.get('edit_date_utc') or date)),
                               event_stamp=int(msg.get('edit_date_utc') or date))
        if report['points'] is not None:matched+=1
        elif report['part']:review+=1
    with connect() as db:
        db.execute('DELETE FROM messages WHERE date_utc < ?', (int(cutoff.timestamp()),))
        db.execute('DELETE FROM review_messages WHERE date_utc < ?', (int(cutoff.timestamp()),))
    print('Общий Telegram: тема «'+result.get('topic_title','')+'», сообщений: '+str(scanned)+
          ', отчётов: '+str(matched)+', на проверку: '+str(review)+'.')



def settings():
    api_id = os.getenv('API_ID', '').strip()
    api_hash = os.getenv('API_HASH', '').strip()
    if not api_id.isdigit() or not api_hash:
        raise ValueError('Заполни API_ID и API_HASH в .env (my.telegram.org).')
    return int(api_id), api_hash


def target_settings(require_topic=True):
    chat_id = os.getenv('CHAT_ID', '').strip()
    topic_id = os.getenv('THREAD_ID', '').strip()
    if not chat_id.lstrip('-').isdigit() or (require_topic and not topic_id.isdigit()):
        raise ValueError('Заполни CHAT_ID и THREAD_ID в .env.')
    return int(chat_id), int(topic_id) if topic_id.isdigit() else None


async def login():
    if use_shared_telegram():
        print('Авторизация выполняется в Bot Manager → Лаунчер → Telegram — общий аккаунт.')
        return
    api_id, api_hash = settings()
    async with TelegramClient(session_source(), api_id, api_hash) as client:
        if not await client.is_user_authorized():
            await client.start(phone=lambda: input('Номер (+380...): ').strip())
        me = await client.get_me()
        if me.bot:
            raise ValueError('Нужна сессия личного аккаунта, не бота. Удали user_history_session.session и повтори вход.')
        print('Авторизация выполнена. Аккаунт ID:', me.id)


async def resolve_chat(client, chat_id):
    """Resolve signed Bot-API-style IDs even when entity cache is empty."""
    try:
        return await client.get_entity(chat_id)
    except (ValueError, TypeError):
        async for dialog in client.iter_dialogs():
            if utils.get_peer_id(dialog.entity) == chat_id:
                return dialog.entity
    raise ValueError(f'Группа CHAT_ID={chat_id} не найдена в личном аккаунте Telegram. Проверь .env и доступ к группе.')


async def read_topics(client, entity):
    """List topic IDs using MTProto; no messages are modified."""
    method = getattr(tl_channels,'GetForumTopicsRequest',None) or getattr(tl_messages,'GetForumTopicsRequest',None)
    if method is None: raise DiagnosticError('topic_api')
    response = await client(method(
        peer=entity, q='', offset_date=0, offset_id=0,
        offset_topic=0, limit=100))
    return [topic for topic in getattr(response, 'topics', ()) if hasattr(topic, 'id')]


def topics_text(topics):
    if not topics:
        return 'Список тем пуст.'
    return '\n'.join(f'  {t.id}: {getattr(t, "title", "(без названия)")}' for t in topics)


async def ensure_topic(client, entity, topic_id):
    """Make sure THREAD_ID is an actual topic ID, not a recent message ID."""
    topics = await read_topics(client, entity)
    found = next((t for t in topics if t.id == topic_id), None)
    # The first page can omit an old/archived topic: retrieve exact topic by ID.
    if found is None:
        by_id = getattr(tl_channels, 'GetForumTopicsByIDRequest', None) or getattr(tl_messages, 'GetForumTopicsByIDRequest', None)
        if by_id is not None:
            response = await client(by_id(peer=entity, topics=[topic_id]))
            found = next((t for t in getattr(response, 'topics', ()) if t.id == topic_id), None)
    if found is None:
        raise ValueError(
            f'Тема THREAD_ID={topic_id} не найдена в группе CHAT_ID={os.getenv("CHAT_ID", "")}. '
            'Проверь .env. Доступные темы (первые 100):\n' + topics_text(topics)
            + '\nПолный список можно посмотреть через list_topics.bat.')
    return found


def belongs_to_topic(msg, topic_id):
    """Conservatively keep *only* messages explicitly linked to this topic.

    Never classify an unthreaded message based on its author/text: this could
    write points from another topic into the Google sheet.
    """
    if msg.id == topic_id:
        return True
    reply = getattr(msg, 'reply_to', None)
    if not reply:
        return False
    return (getattr(reply, 'reply_to_top_id', None) == topic_id or
            getattr(reply, 'reply_to_msg_id', None) == topic_id)


async def iter_selected_messages(client, entity, topic_id, cutoff):
    """Try fast thread API; recover from TOPIC_ID_INVALID with strict filtering."""
    seen = set()
    if topic_id != 1:
        try:
            async for msg in client.iter_messages(entity, reply_to=topic_id, limit=None):
                if msg.date and msg.date < cutoff:
                    break
                if msg.id not in seen:
                    seen.add(msg.id)
                    yield msg
            return
        except RPCError as exc:
            # Telethon may expose an error code, a readable description, or
            # the concrete exception type (TopicIdInvalidError).
            details = ' '.join((str(exc), str(getattr(exc, 'message', '')), type(exc).__name__)).lower()
            if not any(marker in details for marker in (
                    'topic_id_invalid', 'topicidinvalid', 'specified topic id',
                    'msg_id_invalid', 'msgidinvalid', 'message id used')):
                raise
            print('Telegram отклонил GetRepliesRequest для этой темы. '
                  'Перехожу на проверку принадлежности сообщений из истории группы.')
    else:
        raise ValueError('Чтение истории общей темы (THREAD_ID=1) требует отдельного режима. '
                         'Чтобы не смешать сообщения разных тем, синхронизация остановлена.')

    # Safe fallback for an existing non-General forum topic.
    # We only trust Telegram-provided thread metadata.
    accepted = 0
    async for msg in client.iter_messages(entity, limit=None):
        if msg.date and msg.date < cutoff:
            break
        if msg.id not in seen and belongs_to_topic(msg, topic_id):
            accepted += 1
            seen.add(msg.id)
            yield msg
    if not accepted:
        raise ValueError(
            f'Тема {topic_id} существует, но не удалось определить её сообщения '
            'по reply_to_top_id. Данные Google не изменены. Проверь THREAD_ID '
            'через list_topics.bat и при необходимости пришли лог.')


async def authorize_client(client):
    if not await client.is_user_authorized():
        raise DiagnosticError('session_expired')
    me = await client.get_me()
    if me is None: raise DiagnosticError('session_expired')
    if me.bot:
        raise DiagnosticError('bot_account')


async def list_topics_command():
    if use_shared_telegram():
        chat_id,selected=target_settings(require_topic=False)
        result=shared_request('/v1/topics',{'chat_id':chat_id})
        print('Общий Telegram; группа CHAT_ID='+str(chat_id))
        print('Темы:')
        for topic in result.get('topics',[]):
            mark='  [выбрана]' if topic['id']==selected else ''
            print('  '+str(topic['id'])+': '+topic['title']+mark)
        return
    api_id, api_hash = settings()
    chat_id, selected = target_settings(require_topic=False)
    require_session()
    async with TelegramClient(session_source(), api_id, api_hash) as client:
        await authorize_client(client)
        entity = await resolve_chat(client, chat_id)
        listed = await read_topics(client, entity)
        print(f'Группа CHAT_ID={chat_id}; THREAD_ID в .env: {selected if selected is not None else "не задан"}')
        print('Список тем Telegram (первые 100):\n' + topics_text(listed))
        if selected is not None:
            try:
                selected_topic = await ensure_topic(client, entity, selected)
                print(f'Выбрана тема: {selected_topic.id} — {getattr(selected_topic, "title", "")}.')
            except ValueError as exc:
                print('Проверка настройки:', exc)


async def inspect_or_sync(write=False):
    stage='settings'
    passed=[]
    client=None
    try:
        if use_shared_telegram():
            if os.getenv('RENDER'): raise DiagnosticError('shared_gateway')
            if write:
                await asyncio.to_thread(sync_shared)
                return result(passed=('settings','read','database'))
            chat,topic=target_settings()
            response=await asyncio.to_thread(shared_request,'/v1/topics',{'chat_id':chat})
            if not any(t.get('id')==topic for t in response.get('topics',[])):
                raise DiagnosticError('topic_unavailable')
            return result(passed=('settings','group','topic'))
        api_id,api_hash=settings()
        chat_id,topic_id=target_settings()
        if api_id<=0 or chat_id==0 or topic_id<=0: raise DiagnosticError('settings')
        passed.append(stage)
        stage='session'
        if os.getenv('RENDER') and not os.getenv('TELETHON_STRING_SESSION','').strip():
            raise DiagnosticError('session_missing')
        if not os.getenv('TELETHON_STRING_SESSION','').strip() and not Path(SESSION+'.session').exists():
            raise DiagnosticError('session_missing')
        source=session_source()
        client=TelegramClient(source,api_id,api_hash,connection_retries=1,request_retries=1,flood_sleep_threshold=0,timeout=8)
        passed.append(stage)
        stage='connection'
        await client.connect()
        passed.append(stage)
        stage='authorization'
        await authorize_client(client)
        passed.append(stage)
        stage='group'
        entity=await resolve_chat(client,chat_id)
        passed.append(stage)
        stage='topic'
        if topic_id==1: raise DiagnosticError('general_topic')
        await ensure_topic(client,entity,topic_id)
        passed.append(stage)
        stage='read'
        cutoff=datetime.now(timezone.utc)-timedelta(days=30)
        if not write:
            # Probe the exact read path without writing any reports.
            it=iter_selected_messages(client,entity,topic_id,cutoff)
            try:
                await anext(it,None)
            finally:
                await it.aclose()
            passed.append(stage)
            return result(passed=passed)
        from app import connect,save_report,init_db,should_validate_id
        stage='database'
        init_db()
        stage='read'
        async for msg in iter_selected_messages(client,entity,topic_id,cutoff):
            if msg.date is None: continue
            stage='database'
            event=int((msg.edit_date or msg.date).timestamp())
            with connect() as db:
                save_report(db,chat_id,msg.id,topic_id,int(msg.date.timestamp()),msg.message or '',
                            enforce_missing_id=should_validate_id(event),event_stamp=event)
            stage='read'
        passed.append('read')
        stage='database'
        with connect() as db:
            db.execute('DELETE FROM messages WHERE date_utc < ?',(int(cutoff.timestamp()),))
            db.execute('DELETE FROM review_messages WHERE date_utc < ?',(int(cutoff.timestamp()),))
        passed.append(stage)
        return result(passed=passed)
    except Exception as exc:
        return result(stage,classify(stage,exc),passed)
    finally:
        if client is not None:
            try: await asyncio.wait_for(client.disconnect(),timeout=3)
            except Exception: pass

async def sync():
    return await inspect_or_sync(write=True)

if __name__=='__main__':
    command=sys.argv[1] if len(sys.argv)>1 else 'sync'
    if command in ('sync','diagnose'):
        try:
            data=asyncio.run(inspect_or_sync(write=command=='sync'))
        except Exception:
            data=result(None,'unknown')
        print('POINTS_DIAGNOSTIC:'+json.dumps(data,ensure_ascii=False))
        sys.exit(0 if data['ok'] else 1)
    try:
        if command not in ('login','topics'): raise ValueError('Использование: python history_sync.py login | topics | sync | diagnose')
        asyncio.run({'login':login,'topics':list_topics_command}[command]())
    except Exception as exc:
        print('Ошибка:', str(exc))
        sys.exit(1)
