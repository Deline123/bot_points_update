"""Safe, structured diagnostics; never return raw exception or credentials."""
import json
import subprocess
import sys

STAGES=('settings','session','connection','authorization','group','topic','read','database')
STAGE_NAMES={'settings':'Настройки','session':'Сессия','connection':'Подключение','authorization':'Авторизация','group':'Группа','topic':'Тема','read':'Чтение истории','database':'База данных'}
MESSAGES={
 'settings':'Проверьте API_ID, API_HASH, CHAT_ID и THREAD_ID в Render.',
 'session_missing':'Не задана TELETHON_STRING_SESSION.',
 'session_invalid':'Неверный формат строки сессии Telethon.',
 'session_expired':'Сессия не авторизована или отозвана. Нужна новая пользовательская сессия.',
 'session_duplicate':'Telegram отклонил ключ: эта сессия использовалась одновременно с разных IP. Остановите другие экземпляры и создайте отдельную сессию для Render.',
 'bot_account':'Нужна сессия личного Telegram-аккаунта, не бота.',
 'api_invalid':'Telegram отклонил API_ID / API_HASH.',
 'group_unavailable':'Аккаунту недоступна выбранная группа. Проверьте членство и CHAT_ID.',
 'topic_unavailable':'Тема не найдена или недоступна. Проверьте THREAD_ID.',
 'general_topic':'Общая тема (THREAD_ID=1) пока не поддерживается безопасным режимом чтения.',
 'topic_api':'Установленная версия Telethon не содержит нужного метода Telegram для чтения тем.',
 'read_failed':'Не удалось безопасно прочитать сообщения выбранной темы.',
 'flood_wait':'Telegram временно ограничил запросы. Повторите позже.',
 'connection':'Не удалось подключиться к Telegram. Проверьте сеть и доступность Telegram.',
 'timeout':'Telegram не ответил за отведённое время. Повторите проверку.',
 'database':'Ошибка сохранения отчётов в базе данных.',
 'shared_gateway':'Общий Telegram Bot Manager недоступен из облака. Для Render нужна отдельная строка сессии.',
 'busy':'Сейчас выполняется другая операция Telegram. Повторите проверку.',
 'unknown':'Неизвестная ошибка Telethon. Проверьте этап сбоя.',
}
class DiagnosticError(Exception):
    def __init__(self,code): self.code=code; super().__init__(code)

def classify(stage,exc):
    if isinstance(exc,DiagnosticError): return exc.code if exc.code in MESSAGES else 'unknown'
    name=type(exc).__name__
    if name=='AuthKeyDuplicatedError': return 'session_duplicate'
    if name in ('AuthKeyUnregisteredError','SessionRevokedError','SessionExpiredError','AuthKeyInvalidError','AuthKeyNotFound','UnauthorizedError','UserDeactivatedError','UserDeactivatedBanError'): return 'session_expired'
    if name in ('ApiIdInvalidError','ApiIdPublishedFloodError'): return 'api_invalid'
    if name in ('FloodWaitError','FloodPremiumWaitError'): return 'flood_wait'
    if isinstance(exc,TimeoutError): return 'timeout'
    if isinstance(exc,(ConnectionError,OSError)) and stage!='database': return 'connection'
    if stage=='topic' and isinstance(exc,AttributeError): return 'topic_api'
    return {'settings':'settings','session':'session_invalid','connection':'connection',
            'authorization':'session_expired','group':'group_unavailable','topic':'topic_unavailable',
            'read':'read_failed','database':'database'}.get(stage,'unknown')

def result(stage=None,code=None,passed=()):
    return {'ok':code is None,'stage':stage,'code':code,'message':MESSAGES.get(code,'Проверка успешно завершена.'),'checks':[
        {'stage':s,'label':STAGE_NAMES[s],'status':'ok' if s in passed else 'error' if s==stage and code else 'skipped'} for s in STAGES]}

def normalize(payload):
    if not isinstance(payload,dict): return result(None,'unknown')
    stage=payload.get('stage'); code=payload.get('code')
    if stage not in STAGES: stage=None
    passed=[c.get('stage') for c in payload.get('checks',[]) if isinstance(c,dict) and c.get('status')=='ok' and c.get('stage') in STAGES] if isinstance(payload.get('checks'),list) else []
    if payload.get('ok') is True and code is None: return result(passed=passed)
    return result(stage,code if isinstance(code,str) and code in MESSAGES else 'unknown',passed)

def run_history(root,command,timeout):
    try:
        proc=subprocess.run([sys.executable,str(root/'history_sync.py'),command],cwd=root,capture_output=True,text=True,timeout=timeout,stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return result(None,'timeout')
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith('POINTS_DIAGNOSTIC:'):
            try: return normalize(json.loads(line.split(':',1)[1]))
            except (ValueError,TypeError): break
    return result(None,'unknown')
