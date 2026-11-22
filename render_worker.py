"""Independent always-on worker. Never launched by the web server."""
import os
import time
import subprocess
import sys
from app import (connect, init_db, poll_once, state, history_lock, auto_settings, execute_write,
                 get_default_sheet_url, parse_sheet_link, save_history_sync_status, ROOT, CHAT_ID, THREAD_ID, TOKEN)
from runtime_control import record_status, read_status, SharedLock

def configured_history():
    return all(os.getenv(k,'').strip() for k in ('API_ID','API_HASH','TELETHON_STRING_SESSION'))

def safe_record(component, status, **kw):
    return record_status(connect,component,status,**kw)

def history_once():
    if not configured_history():
        safe_record('history','not_configured')
        return False
    if not history_lock.acquire(blocking=False): return False
    try:
        # No interactive login, no raw output in the durable journal.
        p=subprocess.run([sys.executable,str(ROOT/'history_sync.py'),'sync'],cwd=ROOT,
                         timeout=240,stdin=subprocess.DEVNULL,capture_output=True,text=True)
        if p.returncode: raise RuntimeError('Ошибка синхронизации истории')
        save_history_sync_status(success=True,message='Автоматическая синхронизация завершена.')
        safe_record('history','ok')
        return True
    except Exception:
        save_history_sync_status(error='Автоматическая синхронизация не выполнена; проверьте доступ и сессию.')
        safe_record('history','error',error='Не удалось прочитать историю Telegram.')
        return False
    finally:
        history_lock.release()

def cycle(last_history,last_write,now):
    safe_record('worker','ok')
    if history_lock.acquire(blocking=False):
        try:
            poll_once()
            safe_record('telegram','ok',details={'last_update':state.get('last_update')})
        except Exception as exc:
            safe_record('telegram','error',error=str(exc)[:120] if str(exc).startswith('Telegram HTTP ') else 'Ошибка получения Telegram-сообщений.')
        finally:
            history_lock.release()
    if os.getenv('BOT_POINTS_AUTO_HISTORY_ENABLED')=='1' and now-last_history>=900:
        last_history=now
        history_once()
    if now-last_write>=60:
        last_write=now
        cfg=auto_settings()
        if (os.getenv('BOT_POINTS_WRITES_ENABLED')=='1' and os.getenv('BOT_POINTS_CLOUD_WRITES_CONFIRMED')=='1'
                and cfg.get('enabled')):
            try:
                if parse_sheet_link(get_default_sheet_url())!=(cfg.get('sid'),str(cfg.get('gid'))):
                    raise RuntimeError('Configured sheet changed')
                execute_write(cfg['part_col'],cfg['points_col'],cfg['first_row'],cfg['mode'])
            except Exception:
                safe_record('google','error',error='Автозапись остановлена или отложена; проверьте таблицу и настройки.')
        else:
            safe_record('google','disabled')
    safe_record('worker','ok')
    return last_history,last_write

def main():
    if not os.getenv('DATABASE_URL') or os.getenv('BOT_POINTS_WORKER_ENABLED')!='1':
        raise RuntimeError('DATABASE_URL and BOT_POINTS_WORKER_ENABLED=1 required')
    if not TOKEN or not CHAT_ID.lstrip('-').isdigit() or not THREAD_ID.isdigit():
        raise RuntimeError('BOT_TOKEN, CHAT_ID and THREAD_ID required')
    init_db()
    lock=SharedLock(connect,741004)
    history=read_status(connect,'history')
    last_history=history.get('at',0)
    last_write=0
    while True:
        try:
            if lock.acquire(blocking=False):
                try:
                    last_history,last_write=cycle(last_history,last_write,int(time.time()))
                finally:
                    lock.release()
            time.sleep(3)
        except Exception:
            # A transient database outage must not permanently kill the worker.
            time.sleep(10)

if __name__=='__main__': main()
