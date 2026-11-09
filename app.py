import os, re, time, math, sqlite3, threading, logging, csv, io, subprocess, sys, hashlib, json, secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
import requests
from flask import Flask, render_template, request, jsonify
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
from google_sheets_writer import authorize, api_request, read_snapshot, apply_changes, get_service_email, SheetWriteError

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
DB = Path(os.getenv('BOT_POINTS_DB_PATH', str(ROOT / 'points.db'))).expanduser()
if os.getenv('RENDER') and not os.getenv('DATABASE_URL'):
    raise RuntimeError('Set DATABASE_URL for Neon PostgreSQL before Render deployment')
TOKEN = os.getenv('BOT_TOKEN', '').strip()
CHAT_ID = os.getenv('CHAT_ID', '').strip()
THREAD_ID = os.getenv('THREAD_ID', '').strip()
APP_HOST = os.getenv('APP_HOST', '127.0.0.1')
APP_PORT = int(os.getenv('PORT') or os.getenv('APP_PORT', '5000'))
# Optional initial default. Subsequent changes are persisted in points.db.
INITIAL_SHEET_URL = os.getenv('DEFAULT_SHEET_URL', '').strip()
app = Flask(__name__)
from gateway_auth import install_gateway_auth
install_gateway_auth(app)
app.secret_key = os.getenv('FLASK_SECRET_KEY') or os.urandom(32)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('botpoints')
PART_RE = re.compile(r'(?<![A-Z0-9._-])([A-Z0-9]+(?:[._-][A-Z0-9]+)*)(?![A-Z0-9._-])', re.I)

def valid_part(value):
    return len(value) >= 5 and any(c.isalpha() for c in value) and any(c.isdigit() for c in value)

ASSEMBLY_ID_RE = re.compile(r'^\s*Сборка\s*:\s*\*{0,2}(\d{1,12})\*{0,2}\.?\s*$', re.I)


def extract_assembly_id(text):
    """Record optional assembly metadata only; v3.9.6 still never matches by ID."""
    for line in (text or '').splitlines():
        match = ASSEMBLY_ID_RE.fullmatch(line.strip().rstrip('\\').strip())
        if match:
            return match.group(1)
    return None


def part_line_index(text):
    # The assembly ID may precede the part header. It is NOT a points line.
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if ASSEMBLY_ID_RE.fullmatch(line.rstrip('\\').strip()):
            continue
        match = PART_RE.match(line)
        if match and valid_part(match.group(1)):
            return index, match.group(1).upper()
        # Don't scan arbitrary body rows for false-positive part numbers.
        return None, None
    return None, None


def first_part(text):
    return part_line_index(text)[1]
# Every numeric value at the END of a line can contribute points.
# The part-number / worker header is deliberately excluded from the sum.
TRAILING_POINTS_RE = re.compile(
    r'(?<![\d.,])(?P<value>\d+(?:[.,]\d+)?)\s*'
    r'(?:поинт(?:ов|а|ы)?|поінт(?:ів|и)?|балл(?:ов|а)?|points?)?\s*[.!?]*\s*$',
    re.IGNORECASE,
)
SUMMARY_RE = re.compile(
    r'^\s*(?:итого|итог|разом|всего|усього|всього|сумма|сума|в\s+сумме|'
    r'поинтов|поінтів|баллов|балів|итоговые\s+поинты|общий\s+итог)'
    r'(?=\s|[:=\-–—]|\d|$)', re.IGNORECASE,
)
STANDALONE_NUMBER_RE = re.compile(
    r'^\s*\d+(?:[.,]\d+)?\s*'
    r'(?:поинт(?:ов|а|ы)?|поінт(?:ів|и)?|балл(?:ов|а)?|points?)?\s*[.!?]*\s*$', re.IGNORECASE,
)
# Only needed to distinguish a *final component line* from a final result.
# The lines preceding it are counted regardless of their text/label.
COMPONENT_HINTS = (
    'слож', 'склад', 'корпус', 'контрол', 'один', 'кулер', 'вода',
    'сжо', 'башн', 'хаб', 'hub', 'ssd', 'hdd', 'райзер', 'держател',
    'перех', 'экран', 'екран', 'манип', 'маніп', 'доп', 'вентил',
    'подсвет', 'підсвіт', 'кабел', 'помп', 'радиатор', 'радіатор',
    'кроншт', 'установ', 'монтаж', 'провод', 'диск', 'креп', 'кріп',
)

def trailing_points(line):
    m = TRAILING_POINTS_RE.search(line.strip())
    return float(m.group('value').replace(',', '.')) if m else None

def report_result(assembly_id, **fields):
    return dict(fields, assembly_id=assembly_id)

def parse_report(text):
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    header_index, part = part_line_index(text or '')
    assembly_id = extract_assembly_id(text)
    if not part:
        return report_result(assembly_id, part=None, points=None, status='no_part', calculated=None,
                    last=None, reason='Партномер не найден в начале сообщения')

    # Do NOT parse the first line: it contains the part-number, worker name and
    # possibly a worker category such as Ф2, not assembly points.
    body = [line for line in lines[header_index+1:] if not ASSEMBLY_ID_RE.fullmatch(line.rstrip('\\').strip())]
    numbered = [(i, line, trailing_points(line)) for i, line in enumerate(body)]
    numbered = [(i, line, num) for i, line, num in numbered if num is not None]
    if not numbered:
        return report_result(assembly_id, part=part, points=None, status='review', calculated=None,
                    last=None, reason='Не найдены числа в конце строк')

    # A total is expected in the *last nonempty line*. Never double-count it.
    last_line = body[-1]
    last_number = trailing_points(last_line)
    has_summary_word = bool(SUMMARY_RE.match(last_line))
    is_standalone = bool(STANDALONE_NUMBER_RE.fullmatch(last_line))
    before_last = [v for i, _, v in numbered if i != len(body) - 1]
    sum_before = round(sum(before_last), 6)

    # Unlabelled final numbers and labelled totals are unambiguously totals.
    is_total = last_number is not None and (has_summary_word or is_standalone)
    if not is_total and last_number is not None and before_last:
        # Some workers write a custom label or only the final number.
        # Verify it by comparing against all preceding line endings.
        label = last_line[:TRAILING_POINTS_RE.search(last_line).start('value')].casefold()
        is_component = any(hint in label for hint in COMPONENT_HINTS)
        if not is_component and abs(last_number - sum_before) < 1e-6:
            is_total = True
        elif not is_component and last_number >= sum_before:
            # Unknown last-line label: could be an incorrect total or a component.
            # Do not invent a result; surface it for review instead.
            return report_result(assembly_id, part=part, points=None, status='conflict', calculated=sum_before,
                        last=last_number, reason='Неясная последняя строка: число не совпадает с суммой предыдущих')

    if is_total:
        if not before_last:
            # Only the result is present, so accept but say it wasn't cross-checked.
            return report_result(assembly_id, part=part, points=last_number, status='last_only',
                        calculated=None, last=last_number, reason='Последняя строка без разбивки')
        if abs(last_number - sum_before) < 1e-6:
            return report_result(assembly_id, part=part, points=last_number, status='verified',
                        calculated=sum_before, last=last_number,
                        reason='Последняя строка совпадает с суммой предыдущих')
        return report_result(assembly_id, part=part, points=None, status='conflict', calculated=sum_before,
                    last=last_number,
                    reason=f'Расхождение: сумма строк {sum_before:g}, последняя строка {last_number:g}')

    # Last line is another component or contains no ending number: add every
    # component line (no hard-coded list of allowed component names).
    calculated = round(sum(v for _, _, v in numbered), 6)
    return report_result(assembly_id, part=part, points=calculated, status='calculated',
                calculated=calculated, last=last_number,
                reason='Итог не указан — сложены числа в конце строк')

state = {'last_error': None, 'running': False, 'bot_name': None, 'last_update': None}

def connect():
    if os.getenv('DATABASE_URL'):
        from neon_store import connect as pg_connect
        return pg_connect()
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    if os.getenv('DATABASE_URL'):
        from neon_store import init_schema
        return init_schema()
    with connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS messages (chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, thread_id INTEGER, date_utc INTEGER NOT NULL, part TEXT NOT NULL, points REAL NOT NULL, text TEXT NOT NULL, PRIMARY KEY(chat_id,message_id,part))')
        c.execute('CREATE INDEX IF NOT EXISTS idx_part_date ON messages(part,date_utc)')
        c.execute('CREATE TABLE IF NOT EXISTS review_messages (chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, thread_id INTEGER, date_utc INTEGER NOT NULL, part TEXT NOT NULL, status TEXT NOT NULL, calculated REAL, last_value REAL, reason TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(chat_id,message_id))')
        c.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        # One notification per Telegram message; this table is persistent across upgrades.
        c.execute('CREATE TABLE IF NOT EXISTS missing_id_notifications ('
                  'chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, thread_id INTEGER, '
                  'alert_message_id INTEGER, status TEXT NOT NULL, last_error TEXT, created_utc INTEGER NOT NULL, '
                  'PRIMARY KEY(chat_id,message_id))')
        c.execute('CREATE TABLE IF NOT EXISTS sheet_applied (spreadsheet_id TEXT NOT NULL, gid TEXT NOT NULL, part TEXT NOT NULL, fingerprint TEXT NOT NULL, written_value TEXT NOT NULL, updated_utc INTEGER NOT NULL, PRIMARY KEY(spreadsheet_id, gid, part))')
        c.execute('CREATE TABLE IF NOT EXISTS approved_reports (chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, part TEXT NOT NULL, text_hash TEXT NOT NULL, approved_points REAL NOT NULL, approved_utc INTEGER NOT NULL, PRIMARY KEY(chat_id,message_id))')
        c.execute('CREATE TABLE IF NOT EXISTS sheet_message_applied (spreadsheet_id TEXT NOT NULL, gid TEXT NOT NULL, part TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, evidence_hash TEXT NOT NULL, applied_utc INTEGER NOT NULL, PRIMARY KEY(spreadsheet_id,gid,part,chat_id,message_id))')
        c.execute('CREATE TABLE IF NOT EXISTS sheet_pending_jobs (spreadsheet_id TEXT NOT NULL, gid TEXT NOT NULL, payload TEXT NOT NULL, created_utc INTEGER NOT NULL, PRIMARY KEY(spreadsheet_id,gid))')
        # Metadata-only migration. Existing data and lookup keys remain untouched.
        for table in ('messages', 'review_messages'):
            columns = {r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
            if 'assembly_id' not in columns:
                c.execute(f'ALTER TABLE {table} ADD COLUMN assembly_id TEXT')
        c.execute('CREATE TABLE IF NOT EXISTS manual_point_writes ('
                  'operation_id TEXT PRIMARY KEY, spreadsheet_id TEXT NOT NULL, gid TEXT NOT NULL, '
                  'part TEXT NOT NULL, row_number INTEGER NOT NULL, action TEXT NOT NULL, '
                  'old_value TEXT NOT NULL, new_value INTEGER NOT NULL, status TEXT NOT NULL, '
                  'error TEXT, created_utc INTEGER NOT NULL, finished_utc INTEGER)')
        c.execute('CREATE TABLE IF NOT EXISTS sheet_point_reviews ('
                  'spreadsheet_id TEXT NOT NULL, gid TEXT NOT NULL, part TEXT NOT NULL, '
                  'signature TEXT NOT NULL, old_value TEXT NOT NULL, new_value REAL NOT NULL, '
                  'part_col INTEGER NOT NULL, points_col INTEGER NOT NULL, first_row INTEGER NOT NULL, '
                  'mode TEXT NOT NULL, start_stamp INTEGER NOT NULL, end_stamp INTEGER NOT NULL, '
                  'status TEXT NOT NULL, created_utc INTEGER NOT NULL, '
                  'PRIMARY KEY(spreadsheet_id,gid,part))')

def extract(text):
    report = parse_report(text)
    return [(report['part'], report['points'])] if report['points'] is not None else []

def message_hash(part, points, content):
    raw = json.dumps([part, points, content], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def save_report(c, chat_id, msg_id, thread_id, stamp, content, enforce_missing_id=False):
    report = parse_report(content)
    # ID is a validation field only in v3.9.7: Google matching and averages
    # continue to use the part number exactly as before.
    if enforce_missing_id and report['part'] and not report['assembly_id']:
        report = dict(report, points=None, status='missing_id',
                      reason='ID сборки не указан. Исправьте исходное сообщение Telegram.')
    approval = c.execute('SELECT part,text_hash,approved_points FROM approved_reports WHERE chat_id=? AND message_id=?', (chat_id,msg_id)).fetchone()
    raw_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
    if approval:
        if approval['text_hash'] == raw_hash and approval['part'] == report['part']:
            report = dict(report, points=approval['approved_points'], status='approved', reason='Одобрено вручную')
        else:
            # Edited Telegram post: old human approval is no longer valid.
            c.execute('DELETE FROM approved_reports WHERE chat_id=? AND message_id=?', (chat_id,msg_id))
    c.execute('DELETE FROM messages WHERE chat_id=? AND message_id=?', (chat_id, msg_id))
    c.execute('DELETE FROM review_messages WHERE chat_id=? AND message_id=?', (chat_id, msg_id))
    if report['points'] is not None:
        c.execute('INSERT INTO messages(chat_id,message_id,thread_id,date_utc,part,points,text,assembly_id) VALUES (?,?,?,?,?,?,?,?)',
                  (chat_id,msg_id,thread_id,stamp,report['part'],report['points'],content,report['assembly_id']))
    elif report['part']:
        c.execute('INSERT INTO review_messages(chat_id,message_id,thread_id,date_utc,part,status,calculated,last_value,reason,text,assembly_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                  (chat_id,msg_id,thread_id,stamp,report['part'],report['status'],report['calculated'],report['last'],report['reason'],content,report['assembly_id']))
    return report

def matches_scope(chat_id, thread_id):
    return (not CHAT_ID or str(chat_id) == CHAT_ID) and (not THREAD_ID or str(thread_id) == THREAD_ID)

def notification_settings():
    with connect() as c:
        row = c.execute("SELECT value FROM meta WHERE key='missing_id_notifications_settings'").fetchone()
    try:
        value = json.loads(row['value']) if row else {}
    except (ValueError, TypeError):
        value = {}
    return {'enabled': value.get('enabled') is True,
            'enabled_from': int(value.get('enabled_from') or 0)}


def should_validate_id(stamp, settings=None):
    settings = settings if settings is not None else notification_settings()
    return bool(settings['enabled'] and stamp >= settings['enabled_from'])


def send_missing_id_warning(chat_id, msg_id, thread_id, part):
    """Warn only once, in the same Telegram topic, in reply to the source message."""
    now = int(time.time())
    with connect() as c:
        old = c.execute('SELECT * FROM missing_id_notifications WHERE chat_id=? AND message_id=?',
                        (chat_id, msg_id)).fetchone()
        if old and old['status'] in ('sent', 'resolved'):
            return
        if old and old['status'] == 'failed' and now - old['created_utc'] < 300:
            return  # Avoid rapid retries if Telegram temporarily rejects messages.
        c.execute('INSERT INTO missing_id_notifications(chat_id,message_id,thread_id,status,created_utc) '
                  'VALUES (?,?,?,?,?) ON CONFLICT(chat_id,message_id) DO UPDATE SET '
                  'status=excluded.status,created_utc=excluded.created_utc,last_error=NULL',
                  (chat_id,msg_id,thread_id,'sending',now))
    message = ('⚠️ В отчёте отсутствует ID сборки.\n'
               f'Партномер: {part}\n\n'
               'Пожалуйста, отредактируйте исходное сообщение и добавьте строку:\n'
               'Сборка: 52147\n\n'
               'До исправления отчёт не будет учитываться в обновлении Google Таблицы.')
    data = dict(chat_id=chat_id, text=message,
                reply_parameters={'message_id':msg_id, 'allow_sending_without_reply':False})
    if thread_id is not None:
        data['message_thread_id']=int(thread_id)
    try:
        result = api('sendMessage', **data)
        with connect() as c:
            c.execute('UPDATE missing_id_notifications SET status=?,alert_message_id=?,last_error=NULL '
                      'WHERE chat_id=? AND message_id=?',
                      ('sent',result.get('message_id'),chat_id,msg_id))
    except Exception as exc:
        logger.warning('Telegram missing-ID alert failed for %s/%s: %s',chat_id,msg_id,exc)
        with connect() as c:
            c.execute('UPDATE missing_id_notifications SET status=?,last_error=? '
                      'WHERE chat_id=? AND message_id=?',
                      ('failed',str(exc)[:300],chat_id,msg_id))


def resolve_missing_id_warning(chat_id, msg_id):
    with connect() as c:
        row = c.execute('SELECT status,alert_message_id FROM missing_id_notifications '
                        'WHERE chat_id=? AND message_id=?',(chat_id,msg_id)).fetchone()
        if not row or row['status']=='resolved':
            return
        c.execute('UPDATE missing_id_notifications SET status=? WHERE chat_id=? AND message_id=?',
                  ('resolved',chat_id,msg_id))
    # Clean up our own old warning where Telegram permits deletion.
    if row['alert_message_id']:
        try:
            api('deleteMessage', chat_id=chat_id, message_id=row['alert_message_id'])
        except Exception as exc:
            logger.info('Could not delete resolved missing-ID warning: %s',exc)


def store_message(message):
    chat = message.get('chat') or {}
    chat_id = chat.get('id')
    msg_id = message.get('message_id')
    if chat_id is None or msg_id is None: return
    thread_id = message.get('message_thread_id')
    if not matches_scope(chat_id, thread_id): return
    content = message.get('text') or message.get('caption') or ''
    stamp = message.get('date', int(time.time()))
    # Enablement time applies to the event, not old messages in history sync.
    event_stamp = message.get('edit_date', stamp)
    settings = notification_settings()
    validate = should_validate_id(event_stamp, settings)
    with connect() as c:
        report = save_report(c, chat_id, msg_id, thread_id, stamp, content,
                             enforce_missing_id=validate)
    if report['part'] and report['assembly_id']:
        resolve_missing_id_warning(chat_id,msg_id)
    elif validate and report['part'] and not report['assembly_id']:
        send_missing_id_warning(chat_id,msg_id,thread_id,report['part'])
    auto_write_event.set()


@app.route('/api/missing-id-notifications', methods=['GET', 'POST'])
def missing_id_notifications_route():
    if request.method == 'GET':
        cfg=notification_settings()
        return jsonify(**cfg)
    try:
        only_local_write()
        data=request.get_json(silent=True) or {}
        if type(data.get('enabled')) is not bool:
            raise ValueError('Укажи, включены ли уведомления.')
        enabled=data['enabled']
        previous=notification_settings()
        # Keep the original enablement boundary until disabled again.
        since=(previous['enabled_from'] if previous['enabled'] else int(time.time())) if enabled else 0
        value={'enabled':enabled,'enabled_from':since}
        with connect() as c:
            c.execute("INSERT INTO meta(key,value) VALUES('missing_id_notifications_settings',?) "
                      'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (json.dumps(value),))
        return jsonify(**value)
    except PermissionError as exc:
        return jsonify(error=str(exc)),403
    except ValueError as exc:
        return jsonify(error=str(exc)),400


@app.route('/api/assembly-id-column', methods=['GET', 'POST'])
def assembly_id_column_route():
    """Future-only column mapping. Never consulted by Google write/compare."""
    try:
        url = get_default_sheet_url()
        sid, gid = parse_sheet_link(url)
        key = f'assembly_id_column:{sid}:{gid}'
        if request.method == 'GET':
            with connect() as c:
                row=c.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            return jsonify(id_col=int(row['value']) if row else 2, active=False)
        only_local_write()
        data = request.get_json(silent=True) or {}
        col = data.get('id_col')
        if type(col) is not int or not 0 <= col < 100:
            raise ValueError('Укажи корректный столбец ID сборки.')
        with connect() as c:
            c.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) '
                      'DO UPDATE SET value=excluded.value',(key,str(col)))
        return jsonify(id_col=col,active=False)
    except PermissionError as exc:
        return jsonify(error=str(exc)),403
    except (ValueError,SheetWriteError) as exc:
        return jsonify(error=str(exc)),400


def api(method, **params):
    r = requests.post(f'https://api.telegram.org/bot{TOKEN}/{method}', json=params, timeout=45)
    r.raise_for_status()
    body = r.json()
    if not body.get('ok'): raise RuntimeError(body.get('description', 'Telegram API error'))
    return body['result']

def poll():
    state['running'] = True
    while True:
        try:
            if not TOKEN:
                state['last_error'] = 'BOT_TOKEN не указан в .env'
                time.sleep(10); continue
            # Get updates from the configured bot. Only run one instance at a time.
            with connect() as c:
                row = c.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
                offset = int(row['value']) if row else 0
            updates = api('getUpdates', offset=offset, timeout=25, allowed_updates=['message', 'edited_message'])
            for update in updates:
                if 'message' in update: store_message(update['message'])
                if 'edited_message' in update: store_message(update['edited_message'])
                with connect() as c:
                    c.execute("INSERT INTO meta(key,value) VALUES('offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(update['update_id'] + 1),))
                state['last_update'] = datetime.now(timezone.utc).isoformat()
            state['last_error'] = None
        except Exception as exc:
            state['last_error'] = str(exc)
            logger.exception('Telegram polling error')
            time.sleep(5)

@app.route('/healthz')
def healthz():
    return jsonify(status='ok')

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/status')
def status():
    with connect() as c:
        count = c.execute("SELECT COUNT(DISTINCT CAST(chat_id AS TEXT)||':'||CAST(message_id AS TEXT)) AS n FROM messages").fetchone()['n']
        groups = [dict(r) for r in c.execute('SELECT chat_id,thread_id,COUNT(DISTINCT message_id) AS count FROM messages GROUP BY chat_id,thread_id ORDER BY count DESC LIMIT 10')]
    return jsonify(configured=bool(TOKEN), chat_id=CHAT_ID or None, thread_id=THREAD_ID or None, saved_messages=count, scopes=groups, **state)

def parse_sheet_link(url):
    parsed = urlparse(url.strip())
    if parsed.scheme != 'https' or parsed.hostname not in ('docs.google.com',):
        raise ValueError('Нужна ссылка https://docs.google.com/spreadsheets/d/...')
    m = re.fullmatch(r'/spreadsheets/d/([A-Za-z0-9_-]+)(?:/.*)?', parsed.path)
    if not m:
        raise ValueError('Неверная ссылка Google Таблицы')
    gid = parse_qs(parsed.query).get('gid', [None])[0]
    if gid is None:
        gid = parse_qs(parsed.fragment).get('gid', ['0'])[0]
    if not str(gid).isdigit():
        raise ValueError('Неверный gid листа')
    return m.group(1), gid

def get_default_sheet_url():
    """Return saved Google Sheet URL, or optional .env initial configuration."""
    with connect() as c:
        record = c.execute("SELECT value FROM meta WHERE key='default_sheet_url'").fetchone()
    return record['value'] if record else INITIAL_SHEET_URL


def save_default_sheet_url(url):
    """Persist a URL only after its Google CSV has been fetched successfully."""
    previous = get_default_sheet_url()
    with connect() as c:
        if previous and parse_sheet_link(previous) != parse_sheet_link(url):
            # A changed destination must never inherit the old automatic-write toggle.
            c.execute("DELETE FROM meta WHERE key='auto_write_settings'")
        c.execute("INSERT INTO meta (key, value) VALUES ('default_sheet_url', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (url,))


@app.route('/api/sheet-default', methods=['GET'])
def sheet_default():
    return jsonify(url=get_default_sheet_url())


@app.route('/api/sheet', methods=['POST'])
def sheet():
    payload = request.get_json(silent=True) or {}
    try:
        url = payload.get('url', '')
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError('Некорректная ссылка Google Таблицы')
        url = url.strip()
        sid, gid = parse_sheet_link(url)
        try:
            # Existing public CSV path remains fast and does not require credentials.
            endpoint = f'https://docs.google.com/spreadsheets/d/{sid}/export'
            resp = requests.get(endpoint, params={'format':'csv','gid':gid}, timeout=20, allow_redirects=True)
            resp.raise_for_status()
            if len(resp.content) > 5_000_000:
                raise ValueError('Таблица слишком большая (максимум 5 МБ)')
            if 'text/html' in resp.headers.get('content-type', '').lower() or resp.text.lstrip().lower().startswith('<!doctype html'):
                raise ValueError('Google не разрешил публичное чтение CSV')
            rows = list(csv.reader(io.StringIO(resp.content.decode('utf-8-sig'))))
        except (requests.RequestException, UnicodeDecodeError, csv.Error, ValueError):
            # Private sheets can also be viewed once the service account is configured.
            if not get_service_email():
                raise
            with authorize() as session:
                rows = read_snapshot(session, sid, gid)['rows']
        if len(rows) > 10000:
            raise ValueError('Максимум 10 000 строк')
        width = max((len(r) for r in rows), default=0)
        if width > 100:
            raise ValueError('Максимум 100 столбцов')
        save_default_sheet_url(url)
        return jsonify(rows=rows, count=len(rows), width=width, gid=gid)
    except (ValueError, requests.RequestException, UnicodeDecodeError, csv.Error, SheetWriteError) as exc:
        return jsonify(error='Не удалось прочитать таблицу: ' + str(exc)[:240]), 400

@app.route('/api/review-messages', methods=['GET'])
def review_messages():
    clause='date_utc>=?'
    args=[int(time.time())-30*86400]
    if CHAT_ID:
        clause+=' AND chat_id=?'; args.append(int(CHAT_ID))
    if THREAD_ID:
        clause+=' AND thread_id=?'; args.append(int(THREAD_ID))
    with connect() as c:
        rows=[dict(r) for r in c.execute('SELECT chat_id,message_id,date_utc,part,status,calculated,last_value,reason,text,assembly_id FROM review_messages WHERE '+clause+' ORDER BY date_utc DESC LIMIT 500', args)]
        for row in rows:
            row['kind']='message'
            row['text_hash'] = hashlib.sha256(row['text'].encode('utf-8')).hexdigest()
        try:
            sid,gid=parse_sheet_link(get_default_sheet_url())
            queued=c.execute('SELECT * FROM sheet_point_reviews WHERE spreadsheet_id=? AND gid=? AND status=? ORDER BY created_utc DESC LIMIT 500', (sid,str(gid),'pending')).fetchall()
            for r in queued:
                diff=r['new_value']-float(r['old_value'].replace(',','.'))
                sample=c.execute('SELECT text,assembly_id FROM messages WHERE part=? AND date_utc>=? '
                                 'ORDER BY date_utc DESC,message_id DESC LIMIT 1',
                                 (r['part'],int(time.time())-30*86400)).fetchone()
                rows.append(dict(kind='sheet_points',part=r['part'],calculated=r['old_value'],last_value=r['new_value'],
                    reason=f'Увеличение на +{diff:g} поинта (порог +2)',
                    text=sample['text'] if sample else 'Ожидает подтверждения для Google Таблицы',
                    assembly_id=sample['assembly_id'] if sample else None,
                    spreadsheet_id=sid,gid=str(gid),signature=r['signature'], old_value=r['old_value']))
        except ValueError:
            pass
    return jsonify(messages=rows, total=len(rows))

history_lock = threading.Lock()

def history_sync_status():
    with connect() as c:
        row = c.execute("SELECT value FROM meta WHERE key='history_sync_status'").fetchone()
        try:
            record = json.loads(row['value']) if row else {}
        except (TypeError, ValueError):
            record = {}
        since = int(time.time()) - 30 * 86400
        sql = 'SELECT COUNT(*) AS n FROM messages WHERE date_utc>=?'
        review_sql = 'SELECT COUNT(*) AS n FROM review_messages WHERE date_utc>=?'
        args = [since]
        if CHAT_ID:
            sql += ' AND chat_id=?'; review_sql += ' AND chat_id=?'; args.append(int(CHAT_ID))
        if THREAD_ID:
            sql += ' AND thread_id=?'; review_sql += ' AND thread_id=?'; args.append(int(THREAD_ID))
        valid = c.execute(sql, args).fetchone()['n']
        review = c.execute(review_sql, args).fetchone()['n']
    return dict(last_success=record.get('last_success'), last_error=record.get('last_error'),
                last_message=record.get('last_message', ''), valid=valid, review=review,
                period_days=30, thread_configured=bool(CHAT_ID and THREAD_ID),
                source='shared' if os.getenv('BOT_MANAGER_TELEGRAM_SHARED', '').strip()=='1' else 'local')


def save_history_sync_status(*, success=False, message='', error=''):
    current = history_sync_status()
    record = dict(last_success=(datetime.now(timezone.utc).isoformat() if success else current['last_success']),
                  last_message=message[:600] if success else current['last_message'],
                  last_error=error[:350] if error else None)
    with connect() as c:
        c.execute("INSERT INTO meta(key,value) VALUES('history_sync_status', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(record,ensure_ascii=False),))


@app.route('/api/history-status', methods=['GET'])
def get_history_status():
    return jsonify(**history_sync_status())


@app.route('/api/sync-history', methods=['POST'])
def sync_history():
    try:
        only_local_write()
    except PermissionError as exc:
        return jsonify(error=str(exc)),403
    if not history_lock.acquire(blocking=False):
        return jsonify(error='Синхронизация уже выполняется'), 409
    try:
        try:
            proc = subprocess.run([sys.executable, str(ROOT / 'history_sync.py'), 'sync'],
                                  cwd=ROOT, capture_output=True, text=True, timeout=240)
        except subprocess.TimeoutExpired:
            error='Превышено время ожидания (4 минуты). Проверь подключение Telegram.'
            save_history_sync_status(error=error)
            return jsonify(error=error), 504
        output = (proc.stdout + '\n' + proc.stderr).strip()[-2500:]
        if proc.returncode:
            error=output or 'Ошибка чтения истории'
            save_history_sync_status(error=error)
            return jsonify(error=error), 400
        save_history_sync_status(success=True, message=output)
        auto_write_event.set()
        return jsonify(message=output, **history_sync_status())
    except Exception as exc:
        logger.exception('Telegram history sync failed')
        save_history_sync_status(error=str(exc))
        return jsonify(error='Не удалось прочитать историю: '+str(exc)[:180]),500
    finally:
        history_lock.release()

def requested_period(payload):
    try:
        start = payload.get('start') or (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()
        end = payload.get('end') or datetime.now(timezone.utc).date().isoformat()
        start_dt = datetime.strptime(start, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end, '%Y-%m-%d').replace(tzinfo=timezone.utc) + timedelta(days=1)
        if end_dt <= start_dt:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Некорректный диапазон дат')
    if (end_dt-start_dt).days > 32:
        raise ValueError('Максимальный период — 31 день')
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def all_telegram_points(mode, start_stamp, end_stamp):
    """Return every valid part in the selected topic/period, even if absent in Google.

    Iteration is chronological: newly discovered parts are appended in a stable
    first-seen order. Only accepted (non-conflicting) reports are considered.
    """
    if mode not in ('average', 'latest'):
        raise ValueError('Неверный режим поиска')
    query = ('SELECT part, points, date_utc, message_id, chat_id FROM messages '
             'WHERE date_utc >= ? AND date_utc < ?')
    args = [start_stamp, end_stamp]
    if CHAT_ID:
        query += ' AND chat_id=?'
        args.append(int(CHAT_ID))
    if THREAD_ID:
        query += ' AND thread_id=?'
        args.append(int(THREAD_ID))
    query += ' ORDER BY date_utc ASC, message_id ASC'
    grouped = {}
    with connect() as c:
        for row in c.execute(query, args):
            part = row['part'].upper()
            if part not in grouped:
                grouped[part] = {'sum': 0.0, 'count': 0, 'latest': None,
                                 'latest_points': None, 'evidence': []}
            item = grouped[part]
            item['sum'] += row['points']
            item['count'] += 1
            item['latest'] = row['date_utc']
            item['latest_points'] = row['points']
            item['evidence'].append([row['chat_id'],row['message_id'],row['date_utc'],row['points']])
    results = {}
    for part, item in grouped.items():
        val = (math.ceil(item['sum'] / item['count']) if mode == 'average'
               else math.ceil(item['latest_points']))
        evidence = item['evidence'] if mode == 'average' else item['evidence'][-1:]
        digest = hashlib.sha256(json.dumps([mode, evidence], separators=(',', ':')).encode()).hexdigest()
        results[part] = dict(part=part, points=val, count=item['count'], signature=digest,
            latest=datetime.fromtimestamp(item['latest'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    return results


def telegram_points(parts, mode, start_stamp, end_stamp):
    """Return requested Telegram points, with missing parts marked as None."""
    all_points = all_telegram_points(mode, start_stamp, end_stamp)
    return {part: all_points.get(part, dict(part=part, points=None,
                                         count=0, latest=None)) for part in parts}


SHEET_NUMBER_RE = re.compile(r'^\s*([+-]?\d+(?:[.,]\d+)?)\s*$')


def merged_sheet_value(old, new):
    """Keep the old cell if same/missing; otherwise ceil(mean(old, Telegram))."""
    old = '' if old is None else str(old)
    if new is None:
        return old, 'Не найден'
    match = SHEET_NUMBER_RE.fullmatch(old)
    if not match:
        return str(new), 'Заполнен'  # Old points empty or non-numeric
    old_number = float(match.group(1).replace(',', '.'))
    if not math.isfinite(old_number):
        return str(new), 'Заполнен'
    if abs(old_number - new) < 1e-9:
        return old, 'Совпадает'
    return str(math.ceil((old_number + new) / 2)), 'Усреднён'


POINT_REVIEW_THRESHOLD = 2.0


def excess_increase(old, new):
    """Compare the raw Telegram result with the existing Google cell."""
    if new is None:
        return False
    match = SHEET_NUMBER_RE.fullmatch(str(old or ''))
    if not match:
        return False  # Empty/new part has no previous value to compare.
    previous = float(match.group(1).replace(',', '.'))
    return math.isfinite(previous) and new - previous > POINT_REVIEW_THRESHOLD + 1e-9


def sync_point_reviews(snapshot, part_col, points_col, first_row, telegram, sid, gid, mode, start_stamp, end_stamp):
    """Create pending review items; recognize approvals only for unchanged evidence and cells.

    A previously approved report cannot authorize a later unrelated increase.
    """
    blocked = {}
    with connect() as c:
        for index in range(first_row - 1, len(snapshot['rows'])):
            row=snapshot['rows'][index]
            part=normalize_table_part(sheet_cell(row, part_col))
            info=telegram.get(part) if part else None
            if not info:
                continue
            old=sheet_cell(row,points_col)
            new=info['points']
            if not excess_increase(old,new):
                c.execute('DELETE FROM sheet_point_reviews WHERE spreadsheet_id=? AND gid=? AND part=?', (sid,str(gid),part))
                continue
            sig=info['signature']
            previous=c.execute('SELECT * FROM sheet_point_reviews WHERE spreadsheet_id=? AND gid=? AND part=?',
                               (sid,str(gid),part)).fetchone()
            approved=(previous and previous['status']=='approved' and previous['signature']==sig
                      and previous['old_value']==old and previous['new_value']==new
                      and previous['part_col']==part_col and previous['points_col']==points_col
                      and previous['first_row']==first_row and previous['mode']==mode
                      and previous['start_stamp']==start_stamp and previous['end_stamp']==end_stamp)
            if approved:
                continue
            c.execute('INSERT INTO sheet_point_reviews (spreadsheet_id,gid,part,signature,old_value,new_value,'
                      'part_col,points_col,first_row,mode,start_stamp,end_stamp,status,created_utc) '
                      'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(spreadsheet_id,gid,part) DO UPDATE SET '
                      'signature=excluded.signature,old_value=excluded.old_value,new_value=excluded.new_value,'
                      'part_col=excluded.part_col,points_col=excluded.points_col,first_row=excluded.first_row,'
                      'mode=excluded.mode,start_stamp=excluded.start_stamp,end_stamp=excluded.end_stamp,'
                      'status=excluded.status,created_utc=excluded.created_utc',
                      (sid,str(gid),part,sig,old,new,part_col,points_col,first_row,mode,start_stamp,end_stamp,'pending',int(time.time())))
            blocked[part]={'old':old,'new':new,'signature':sig}
    return blocked


def safe_tsv_cell(cell):
    """Avoid introducing extra rows/columns when copying sheet cell contents."""
    return str(cell).replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')


def combine_sheet_with_telegram(normalized, telegram_all):
    """Keep Google row order, update matched values, append new Telegram parts.

    normalized = list of (raw_part, normalized_part_or_None, old_value).
    Original blank rows BETWEEN entries are kept; empty rows at the very end
    are trimmed so new part numbers are appended directly after the last entry.
    """
    rows = list(normalized)
    while rows and not rows[-1][0] and not rows[-1][2].strip():
        rows.pop()
    existing_parts = {part for _, part, _ in rows if part}
    results = []
    for raw_part, part, old in rows:
        telegram = telegram_all.get(part, {}).get('points') if part else None
        if not raw_part:
            result, state = old, 'Пустая строка' if not old.strip() else 'Без партномера'
        elif part is None:
            result, state = old, 'Неверный партномер'
        elif excess_increase(old, telegram):
            result, state = old, 'На проверке'
        else:
            result, state = merged_sheet_value(old, telegram)
        results.append(dict(part=raw_part, old=old, telegram=telegram,
                            output=result, status=state))
    additions = []
    for part, info in telegram_all.items():
        if part not in existing_parts:
            item = dict(part=part, old='', telegram=info['points'],
                        output=str(info['points']), status='Новый партномер')
            results.append(item)
            additions.append(item)
    return dict(
        results=results,
        # Two-column output can be pasted into adjacent Part / Points columns.
        two_columns='\n'.join(safe_tsv_cell(r['part']) + '\t' + safe_tsv_cell(r['output'])
                              for r in results),
        # Separate columns work even if Google's columns are NOT adjacent.
        column='\n'.join(safe_tsv_cell(r['output']) for r in results),
        part_column='\n'.join(safe_tsv_cell(r['part']) for r in results),
        added_rows=additions,
        new_pairs='\n'.join(part + '\t' + str(r['output'])
                            for r in additions for part in [r['part']]),
        total=len(results),
        original=len(rows),
        added=len(additions),
        changed=sum(r['status'] in ('Усреднён', 'Заполнен') for r in results),
        unchanged=sum(r['status'] == 'Совпадает' for r in results),
        missing=sum(r['status'] == 'Не найден' for r in results),
        held=sum(r['status'] == 'На проверке' for r in results),
    )


@app.route('/api/compare', methods=['POST'])
def compare_sheet():
    """Return original sheet rows updated + new Telegram parts appended."""
    payload = request.get_json(silent=True) or {}
    rows = payload.get('rows', [])
    if not isinstance(rows, list) or len(rows) > 10000:
        return jsonify(error='Максимум 10 000 строк'), 400
    normalized = []
    for item in rows:
        if not isinstance(item, dict):
            return jsonify(error='Неверный формат строки'), 400
        raw_part, old = item.get('part', ''), item.get('old', '')
        if not isinstance(raw_part, str) or not isinstance(old, (str, int, float)):
            return jsonify(error='Неверное значение в строке'), 400
        raw_part = raw_part.strip()
        if len(raw_part) > 250 or len(str(old)) > 1000:
            return jsonify(error='Слишком длинное значение в таблице'), 400
        normalized.append((raw_part, first_part(raw_part) if raw_part else None, str(old)))
    try:
        start_stamp, end_stamp = requested_period(payload)
        telegram_all = all_telegram_points(payload.get('mode', 'average'), start_stamp, end_stamp)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    # Guard against accidentally producing a huge pasted result.
    if len(telegram_all) > 10000:
        return jsonify(error='Слишком много уникальных партномеров Telegram'), 400
    return jsonify(**combine_sheet_with_telegram(normalized, telegram_all))


# Every individual Telegram message is counted at most once per sheet. Messages
# approved by a human enter the same ledger as automatically validated reports.
write_lock = threading.Lock()
prepared_writes = {}
PREVIEW_SECONDS = 15 * 60
auto_write_event = threading.Event()
auto_state = {'last_success': None, 'last_error': None, 'last_updated_parts': 0, 'last_updated_cells': 0}

def snapshot_fingerprint(snapshot):
    return hashlib.sha256(json.dumps([snapshot['rows'], snapshot['formulas']], ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def current_written_fingerprints(spreadsheet_id, gid):
    with connect() as c:
        return {r['part']: r['fingerprint'] for r in c.execute(
            'SELECT part, fingerprint FROM sheet_applied WHERE spreadsheet_id=? AND gid=?',
            (spreadsheet_id, str(gid)))}


def sheet_cell(row, col):
    return str(row[col]) if len(row) > col and row[col] is not None else ''


def normalize_table_part(raw):
    # Match the whole cell, not merely a prefix of a longer/ambiguous label.
    cleaned = raw.strip().upper()
    return cleaned if PART_RE.fullmatch(cleaned) and valid_part(cleaned) else None


def build_write_preview(snapshot, part_col, points_col, first_row, telegram_all, applied, blocked=None):
    """Pure, testable planning. Existing rows stay put; new ones start after last occupied row."""
    if not isinstance(part_col, int) or not isinstance(points_col, int) or part_col == points_col or not 0 <= part_col < 100 or not 0 <= points_col < 100:
        raise ValueError('Выбери два разных столбца (до 100)')
    if not isinstance(first_row, int) or first_row < 1 or first_row > len(snapshot['rows'])+1:
        raise ValueError('Неверный номер первой строки данных')
    blocked=blocked or {}
    existing, rows = set(), []
    updates = []
    outcomes = []
    for line in range(first_row, len(snapshot['rows'])+1):
        source = snapshot['rows'][line-1]
        raw, old = sheet_cell(source, part_col), sheet_cell(source, points_col)
        part = normalize_table_part(raw)
        if part in existing and part is not None:
            raise ValueError('В Google Таблице повторяется партномер ' + part + '. Сначала исправь дубли.')
        if part:
            existing.add(part)
        info = telegram_all.get(part) if part else None
        telegram = info['points'] if info else None
        if part and info and part in blocked:
            output, state = old, 'На проверке'
        elif part and info and applied.get(part) == info['signature']:
            output, state = old, 'Уже учтён'
        elif not raw.strip():
            output, state = old, 'Пустая строка'
        elif part is None:
            output, state = old, 'Неверный партномер'
        else:
            output, state = merged_sheet_value(old, telegram)
        if state in ('Усреднён', 'Заполнен'):
            formula_row = snapshot['formulas'][line-1] if line <= len(snapshot['formulas']) else []
            if sheet_cell(formula_row, points_col).startswith('='):
                raise ValueError(f'В {line}-й строке поинты вычисляются формулой. Не буду заменять формулу.')
            updates.append((line, points_col, int(output)))
            outcomes.append((part, info['signature'], str(output)))
        rows.append(dict(row=line, part=raw, old=old, telegram=telegram, output=output, status=state))
    last = len(snapshot['rows'])
    added_count = 0
    for part, info in telegram_all.items():
        if part in existing:
            continue
        last += 1
        if last > 10000:
            raise ValueError('После добавления партномеров будет больше 10 000 строк')
        point = int(info['points'])
        updates.extend([(last, part_col, part), (last, points_col, point)])
        outcomes.append((part, info['signature'], str(point)))
        rows.append(dict(row=last, part=part, old='', telegram=point, output=str(point), status='Новый партномер'))
        added_count += 1
    if len(updates) > 4000:
        raise ValueError('Слишком много изменений для одной операции (более 4000 ячеек)')
    return dict(
        results=rows,
        updates=updates,
        outcomes=outcomes,
        changed=sum(r['status'] in ('Заполнен','Усреднён') for r in rows),
        added=added_count,
        unchanged=sum(r['status'] in ('Совпадает','Уже учтён') for r in rows),
        original=len(snapshot['rows']) - first_row + 1,
        total=len(rows),
        missing=sum(r['status']=='Не найден' for r in rows),
        held=sum(r['status']=='На проверке' for r in rows),
        two_columns='\n'.join(safe_tsv_cell(r['part'])+'\t'+safe_tsv_cell(r['output']) for r in rows),
        part_column='\n'.join(safe_tsv_cell(r['part']) for r in rows),
        column='\n'.join(safe_tsv_cell(r['output']) for r in rows),
        new_pairs='\n'.join(r['part']+'\t'+str(r['output']) for r in rows if r['status']=='Новый партномер'),
    )




def label_processed_rows(preview,sid,gid):
    with connect() as c:
        done={r['part'] for r in c.execute('SELECT DISTINCT part FROM sheet_message_applied WHERE spreadsheet_id=? AND gid=?', (sid,str(gid)))}
    for row in preview['results']:
        if row['status']=='Не найден' and normalize_table_part(row['part']) in done:
            row['status']='Уже учтён'
            preview['missing']-=1
            preview['unchanged']+=1
    return preview


def only_local_write():
    if request.remote_addr not in ('127.0.0.1', '::1'):
        raise PermissionError('Изменять таблицу можно только с этого компьютера')
    origin = request.headers.get('Origin')
    if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
        raise PermissionError('Недопустимый Origin запроса')
    if request.headers.get('Sec-Fetch-Site') == 'cross-site':
        raise PermissionError('Недопустимый запрос из другого сайта')


def auto_settings():
    with connect() as c:
        row = c.execute("SELECT value FROM meta WHERE key='auto_write_settings'").fetchone()
    return json.loads(row['value']) if row else {'enabled': False}


def write_settings(value):
    with connect() as c:
        c.execute("INSERT INTO meta(key,value) VALUES('auto_write_settings',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps(value, ensure_ascii=False),))


def extract_options(payload):
    pc, vc, first = payload.get('part_col'), payload.get('points_col'), payload.get('first_row')
    if type(pc) is not int or type(vc) is not int or type(first) is not int:
        raise ValueError('Укажи два столбца и номер первой строки')
    if not 0 <= pc < 100 or not 0 <= vc < 100 or pc == vc or not 1 <= first <= 10000:
        raise ValueError('Неверные столбцы или первая строка')
    mode = payload.get('mode','average')
    if mode not in ('average','latest'):
        raise ValueError('Неверный режим среднего')
    return pc,vc,first,mode


def migration_baseline(sid, gid):
    """Mark old v3.8 writes as accounted for using their recorded write time.

    v3.8 only kept a per-part signature, not individual message IDs. The old
    successful write time is a conservative migration boundary for existing
    entries. New entries after that time remain eligible for processing.
    """
    key = 'ledger_migration_v39:' + sid + ':' + str(gid)
    with connect() as c:
        if c.execute('SELECT 1 FROM meta WHERE key=?',(key,)).fetchone():
            return
        old = list(c.execute('SELECT part,updated_utc FROM sheet_applied WHERE spreadsheet_id=? AND gid=?', (sid,str(gid))))
        for item in old:
            q = ('SELECT part,chat_id,message_id,points,text FROM messages '
                 'WHERE part=? AND date_utc<=?')
            args = [item['part'], item['updated_utc']]
            if CHAT_ID:
                q += ' AND chat_id=?'; args.append(int(CHAT_ID))
            if THREAD_ID:
                q += ' AND thread_id=?'; args.append(int(THREAD_ID))
            for r in c.execute(q, args).fetchall():
                c.execute('INSERT OR IGNORE INTO sheet_message_applied '
                    '(spreadsheet_id,gid,part,chat_id,message_id,evidence_hash,applied_utc) VALUES (?,?,?,?,?,?,?)',
                    (sid,str(gid),r['part'],r['chat_id'],r['message_id'],
                     message_hash(r['part'],r['points'],r['text']),int(time.time())))
        c.execute('INSERT INTO meta(key,value) VALUES (?,?)', (key,'1'))


def pending_telegram_points(sid, gid, mode, start_stamp, end_stamp, only_messages=None):
    """Only messages not yet incorporated into the selected spreadsheet."""
    if mode not in ('average','latest'):
        raise ValueError('Неверный режим')
    migration_baseline(sid, gid)
    q = ('SELECT m.part,m.points,m.text,m.chat_id,m.message_id,m.date_utc,a.evidence_hash '
         'FROM messages m LEFT JOIN sheet_message_applied a ON '
         'a.spreadsheet_id=? AND a.gid=? AND a.part=m.part AND '
         'a.chat_id=m.chat_id AND a.message_id=m.message_id '
         'WHERE m.date_utc>=? AND m.date_utc<?')
    args = [sid,str(gid),start_stamp,end_stamp]
    if CHAT_ID:
        q += ' AND m.chat_id=?'; args.append(int(CHAT_ID))
    if THREAD_ID:
        q += ' AND m.thread_id=?'; args.append(int(THREAD_ID))
    q += ' ORDER BY m.date_utc,m.message_id'
    grouped = {}
    with connect() as c:
        for r in c.execute(q, args):
            if only_messages is not None and (r['chat_id'],r['message_id']) not in only_messages:
                continue
            proof = message_hash(r['part'],r['points'],r['text'])
            if proof == r['evidence_hash']:
                continue
            part = r['part']
            item = grouped.setdefault(part,{'sum':0,'count':0,'latest':None,'evidence':[]})
            item['sum'] += r['points']; item['count'] += 1
            item['latest'] = r['points']
            item['evidence'].append(dict(part=part,chat_id=r['chat_id'],message_id=r['message_id'],hash=proof))
    results = {}
    for part,item in grouped.items():
        value = math.ceil(item['sum']/item['count']) if mode=='average' else math.ceil(item['latest'])
        signature = hashlib.sha256(json.dumps(item['evidence'],sort_keys=True).encode()).hexdigest()
        results[part] = dict(part=part,points=value,count=item['count'],signature=signature,
                             evidence=item['evidence'])
    return results


def mark_processed(c,sid,gid,telegram):
    now=int(time.time())
    for part,item in telegram.items():
        for record in item['evidence']:
            c.execute('INSERT INTO sheet_message_applied '
                '(spreadsheet_id,gid,part,chat_id,message_id,evidence_hash,applied_utc) VALUES (?,?,?,?,?,?,?) '
                'ON CONFLICT(spreadsheet_id,gid,part,chat_id,message_id) DO UPDATE SET '
                'evidence_hash=excluded.evidence_hash,applied_utc=excluded.applied_utc',
                (sid,str(gid),part,record['chat_id'],record['message_id'],record['hash'],now))


def evidence_fingerprint(telegram):
    return hashlib.sha256(json.dumps([(p, i['signature']) for p,i in sorted(telegram.items())],
                                      separators=(',',':')).encode()).hexdigest()


def sheet_cell_value(snapshot,row,col):
    rows=snapshot['rows']
    return sheet_cell(rows[row-1],col) if row<=len(rows) else ''


def pending_job(sid,gid):
    with connect() as c:
        r=c.execute('SELECT payload FROM sheet_pending_jobs WHERE spreadsheet_id=? AND gid=?',(sid,str(gid))).fetchone()
    return json.loads(r['payload']) if r else None


def finish_job(sid,gid,job):
    with connect() as c:
        mark_processed(c,sid,gid,job['telegram'])
        for part, signature, output in job['outcomes']:
            c.execute('INSERT INTO sheet_applied (spreadsheet_id,gid,part,fingerprint,written_value,updated_utc) VALUES (?,?,?,?,?,?) '
                'ON CONFLICT(spreadsheet_id,gid,part) DO UPDATE SET fingerprint=excluded.fingerprint, '
                'written_value=excluded.written_value,updated_utc=excluded.updated_utc',
                (sid,str(gid),part,signature,output,int(time.time())))
        c.execute('DELETE FROM sheet_pending_jobs WHERE spreadsheet_id=? AND gid=?',(sid,str(gid)))


def reconcile_job(session,sid,gid):
    """Recover an ambiguous Google write after a timeout/crash without averaging twice."""
    job=pending_job(sid,gid)
    if job is None:
        return
    fresh=read_snapshot(session,sid,gid)
    states=[]
    for row,col,old,new in job['cells']:
        now=sheet_cell_value(fresh,row,col)
        if now==str(new):
            states.append('new')
        elif now==str(old):
            states.append('old')
        else:
            states.append('conflict')
    if states and all(s=='new' for s in states):
        finish_job(sid,gid,job)
        return
    if all(s=='old' for s in states):
        # Confirmed not applied; discard the ambiguous journal for fresh planning.
        with connect() as c:
            c.execute('DELETE FROM sheet_pending_jobs WHERE spreadsheet_id=? AND gid=?',(sid,str(gid)))
        return
    raise SheetWriteError('Предыдущая запись выполнена частично или таблица изменилась. '
        'Автозапись остановлена для защиты данных; проверь Google Таблицу вручную.')


def run_write(pc,vc,first,mode='average',only_messages=None,period=None,expected_evidence=None,only_parts=None):
    url=get_default_sheet_url()
    sid,gid=parse_sheet_link(url)
    start,end=period if period is not None else (int(time.time())-30*86400,int(time.time())+86400)
    # Check SQLite first: an idle bot should not poll the Google API every minute.
    pending=pending_telegram_points(sid,gid,mode,start,end,only_messages)
    if only_parts is not None:
        pending={part:info for part,info in pending.items() if part in only_parts}
    if expected_evidence is not None and evidence_fingerprint(pending)!=expected_evidence:
        raise SheetWriteError('Данные Telegram изменились после сравнения. Повтори сравнение.')
    with authorize() as session:
        reconcile_job(session,sid,gid)
        # Recovery can have accounted for evidence already; recompute after recovery.
        pending=pending_telegram_points(sid,gid,mode,start,end,only_messages)
        if only_parts is not None:
            pending={part:info for part,info in pending.items() if part in only_parts}
        if expected_evidence is not None and evidence_fingerprint(pending)!=expected_evidence:
            raise SheetWriteError('Предыдущая запись уже применена или Telegram обновлён. Повтори сравнение.')
        if not pending:
            return dict(ok=True, updated_parts=0, updated_cells=0, added=0, changed=0,
                        message='Новых отчётов для записи нет')
        snapshot=read_snapshot(session,sid,gid)
        blocked=sync_point_reviews(snapshot,pc,vc,first,pending,sid,gid,mode,start,end)
        preview=build_write_preview(snapshot,pc,vc,first,pending,{},blocked)
        permitted={part:info for part,info in pending.items() if part not in blocked}
        # Read again to prevent writes based on stale data (including formulas).
        fresh=read_snapshot(session,sid,gid)
        if snapshot_fingerprint(fresh)!=snapshot_fingerprint(snapshot):
            raise SheetWriteError('Google Таблица изменилась во время расчёта. Повтори запись.')
        cells = [[row,col,sheet_cell_value(snapshot,row,col),value] for row,col,value in preview['updates']]
        job={'cells':cells,'telegram':permitted,'outcomes':preview['outcomes']}
        with connect() as c:
            c.execute('INSERT INTO sheet_pending_jobs(spreadsheet_id,gid,payload,created_utc) VALUES (?,?,?,?)',
                      (sid,str(gid),json.dumps(job,ensure_ascii=False),int(time.time())))
        if preview['updates']:
            response=apply_changes(session,snapshot,preview['updates'])
        else:
            response={'totalUpdatedCells':0}
        finish_job(sid,gid,job)
        return dict(ok=True, updated_parts=len(permitted), updated_cells=response.get('totalUpdatedCells',len(preview['updates'])),
                    added=preview['added'],changed=preview['changed'],held=len(blocked),
                    message=('Google Таблица обновлена' if not blocked else f'Доступные значения записаны; на проверке: {len(blocked)}'))


def execute_write(pc,vc,first,mode='average',only_messages=None,period=None,expected_evidence=None,only_parts=None):
    if not write_lock.acquire(blocking=False):
        raise SheetWriteError('Запись уже выполняется. Повтори действие позже.')
    try:
        result=run_write(pc,vc,first,mode,only_messages,period,expected_evidence,only_parts)
        auto_state['last_error']=None
        auto_state['last_success']=datetime.now(timezone.utc).isoformat()
        auto_state['last_updated_parts']=result['updated_parts']
        auto_state['last_updated_cells']=result['updated_cells']
        return result
    except Exception as exc:
        auto_state['last_error']=str(exc)[:250]
        raise
    finally:
        write_lock.release()



# Manual changes are exact administrator decisions. ID-based matching is intentionally
# NOT activated in v3.9.8; only selected Part/Points columns are touched.
manual_previews = {}
MANUAL_PREVIEW_SECONDS = 10 * 60


def manual_input(payload):
    if not isinstance(payload, dict):
        raise ValueError('Некорректный запрос')
    raw = payload.get('part')
    points = payload.get('points')
    if not isinstance(raw, str) or len(raw) > 250:
        raise ValueError('Введи партномер')
    part = normalize_table_part(raw)
    if not part:
        raise ValueError('Некорректный партномер. Пример: R5G23506')
    # Reject floats, booleans and arbitrary strings to avoid accidental coercion.
    if type(points) is not int or not 0 <= points <= 1000:
        raise ValueError('Введи целое число поинтов от 0 до 1000')
    pc,vc,first,_ = extract_options(payload)
    target = payload.get('target_row')
    if target is not None and (type(target) is not int or not 1 <= target <= 10000):
        raise ValueError('Некорректный номер строки')
    return part,points,pc,vc,first,target


def plan_manual_write(snapshot, part, points, pc, vc, first, target=None):
    if not 0 <= pc < 100 or not 0 <= vc < 100 or pc == vc:
        raise ValueError('Столбцы партномера и поинтов должны различаться')
    if not 1 <= first <= len(snapshot['rows']) + 1:
        raise ValueError('Проверь первую строку данных')
    matches=[]
    for rownum in range(first,len(snapshot['rows'])+1):
        row=snapshot['rows'][rownum-1]
        if normalize_table_part(sheet_cell(row,pc)) == part:
            matches.append(dict(row=rownum,old=sheet_cell(row,vc)))
    if len(matches)>1 and target is None:
        return dict(action='choose',part=part,points=points,candidates=matches)
    if target is not None and target not in [v['row'] for v in matches]:
        raise ValueError('Выбранная строка больше не соответствует партномеру')
    if matches:
        chosen=next(v for v in matches if target is None or v['row']==target)
        rownum=chosen['row'];old=chosen['old']
        match = SHEET_NUMBER_RE.fullmatch(old)
        if (match and abs(float(match.group(1).replace(',', '.')) - points)<1e-9):
            return dict(action='unchanged',part=part,points=points,row=rownum,old=old,candidates=matches)
        formula=(snapshot['formulas'][rownum-1] if rownum<=len(snapshot['formulas']) else [])
        if sheet_cell(formula,vc).startswith('='):
            raise ValueError(f'В строке {rownum} поинты рассчитываются формулой. Ручная замена запрещена.')
        return dict(action='replace',part=part,points=points,row=rownum,old=old,candidates=matches)
    if target is not None:
        raise ValueError('Строка не найдена. Повтори предварительную проверку.')
    rownum=len(snapshot['rows'])+1
    if rownum>10000:
        raise ValueError('Достигнут лимит таблицы: 10 000 строк')
    return dict(action='add',part=part,points=points,row=rownum,old='',candidates=[])


def ensure_manual_columns_match_auto(sid,gid,pc,vc,first):
    cfg=auto_settings()
    if cfg.get('enabled') and cfg.get('sid')==sid and str(cfg.get('gid'))==str(gid):
        if (pc,vc,first)!=(cfg.get('part_col'),cfg.get('points_col'),cfg.get('first_row')):
            raise ValueError('Столбцы ручной записи отличаются от включённого автообновления. '
                             'Сначала выбери столбцы автообновления или выключи его.')


def manual_write_history_rows(limit=15):
    with connect() as c:
        rows=c.execute('SELECT created_utc,part,row_number,action,old_value,new_value,status '
                       'FROM manual_point_writes ORDER BY created_utc DESC, rowid DESC LIMIT ?', (limit,)).fetchall()
    return [dict(row) for row in rows]


@app.route('/api/manual-write-history',methods=['GET'])
def manual_write_history_route():
    return jsonify(items=manual_write_history_rows())


@app.route('/api/manual-prepare',methods=['POST'])
def manual_prepare_route():
    try:
        only_local_write()
        part,points,pc,vc,first,target=manual_input(request.get_json(silent=True) or {})
        sid,gid=parse_sheet_link(get_default_sheet_url())
        ensure_manual_columns_match_auto(sid,gid,pc,vc,first)
        if not write_lock.acquire(blocking=False):
            raise SheetWriteError('Сейчас выполняется другая запись. Повтори позже.')
        try:
            with authorize() as session:
                reconcile_job(session,sid,gid)
                snapshot=read_snapshot(session,sid,gid)
            plan=plan_manual_write(snapshot,part,points,pc,vc,first,target)
            if plan['action'] in ('unchanged','choose'):
                return jsonify(**plan,can_apply=False)
            token=secrets.token_urlsafe(32)
            with write_lock_plan_lock:
                manual_previews.clear()
                manual_previews[token]=dict(created=time.time(),sid=sid,gid=gid,
                    fingerprint=snapshot_fingerprint(snapshot),part=part,points=points,
                    pc=pc,vc=vc,first=first,target=target,action=plan['action'],row=plan['row'])
            return jsonify(**plan,can_apply=True,plan_token=token)
        finally:
            write_lock.release()
    except PermissionError as exc:
        return jsonify(error=str(exc)),403
    except (ValueError,SheetWriteError) as exc:
        return jsonify(error=str(exc)),400
    except Exception:
        logger.exception('Manual preview error')
        return jsonify(error='Ошибка подготовки ручной записи. Проверь журнал бота.'),500


write_lock_plan_lock = threading.Lock()


@app.route('/api/manual-apply',methods=['POST'])
def manual_apply_route():
    try:
        only_local_write()
        payload=request.get_json(silent=True) or {}
        token=payload.get('plan_token')
        if not isinstance(token,str):
            raise ValueError('Сначала выполни предварительную проверку')
        with write_lock_plan_lock:
            plan=manual_previews.get(token)
        if not plan or time.time()-plan['created']>MANUAL_PREVIEW_SECONDS:
            return jsonify(error='Подтверждение устарело. Проверь данные заново.'),409
        if parse_sheet_link(get_default_sheet_url())!=(plan['sid'],plan['gid']):
            return jsonify(error='Основная Google Таблица изменилась. Проверь заново.'),409
        if not write_lock.acquire(blocking=False):
            return jsonify(error='Сейчас выполняется другая запись. Повтори позже.'),409
        try:
            with write_lock_plan_lock:
                plan=manual_previews.pop(token,None)
            if not plan:
                return jsonify(error='Это подтверждение уже использовано.'),409
            sid,gid=plan['sid'],plan['gid']
            ensure_manual_columns_match_auto(sid,gid,plan['pc'],plan['vc'],plan['first'])
            with authorize() as session:
                reconcile_job(session,sid,gid)
                fresh=read_snapshot(session,sid,gid)
                if snapshot_fingerprint(fresh)!=plan['fingerprint']:
                    return jsonify(error='Google Таблица изменилась после проверки. Ничего не записано.'),409
                actual=plan_manual_write(fresh,plan['part'],plan['points'],plan['pc'],plan['vc'],plan['first'],plan['target'])
                if actual['action']!=plan['action'] or actual.get('row')!=plan['row']:
                    return jsonify(error='Строка изменилась. Проверь данные заново.'),409
                if plan['action']=='add':
                    changes=[(actual['row'],plan['pc'],plan['part']), (actual['row'],plan['vc'],plan['points'])]
                else:
                    changes=[(actual['row'],plan['vc'],plan['points'])]
                operation=secrets.token_hex(16)
                # An explicit manual choice supersedes only *currently pending* Telegram
                # evidence for that part. Future messages continue to be processed.
                start=int(time.time())-30*86400
                end=int(time.time())+86400
                pending=pending_telegram_points(sid,gid,'average',start,end)
                with connect() as c:
                    c.execute('INSERT INTO manual_point_writes('
                              'operation_id,spreadsheet_id,gid,part,row_number,action,old_value,new_value,status,created_utc) '
                              'VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (operation,sid,str(gid),plan['part'],plan['row'],plan['action'],
                               actual['old'],plan['points'],'pending',int(time.time())))
                try:
                    response=apply_changes(session,fresh,changes)
                except Exception as exc:
                    with connect() as c:
                        c.execute('UPDATE manual_point_writes SET status=?,error=?,finished_utc=? WHERE operation_id=?',
                                  ('uncertain',str(exc)[:300],int(time.time()),operation))
                    raise SheetWriteError('Google не подтвердил запись. Проверь значение в таблице перед повторной попыткой. '+str(exc)[:120]) from exc
                with connect() as c:
                    if plan['part'] in pending:
                        mark_processed(c,sid,gid,{plan['part']:pending[plan['part']]})
                    c.execute('UPDATE sheet_point_reviews SET status=? WHERE spreadsheet_id=? AND gid=? AND part=?',
                              ('superseded',sid,str(gid),plan['part']))
                    c.execute('INSERT INTO sheet_applied(spreadsheet_id,gid,part,fingerprint,written_value,updated_utc) '
                              'VALUES(?,?,?,?,?,?) ON CONFLICT(spreadsheet_id,gid,part) DO UPDATE SET '
                              'fingerprint=excluded.fingerprint,written_value=excluded.written_value,updated_utc=excluded.updated_utc',
                              (sid,str(gid),plan['part'],'manual:'+operation,str(plan['points']),int(time.time())))
                    c.execute('UPDATE manual_point_writes SET status=?,finished_utc=? WHERE operation_id=?',
                              ('applied',int(time.time()),operation))
                auto_write_event.set()
                return jsonify(ok=True,action=plan['action'],row=plan['row'],part=plan['part'],
                               points=plan['points'],updated_cells=response.get('totalUpdatedCells',len(changes)),
                               message=('Строка добавлена в Google Таблицу' if plan['action']=='add' else
                                        'Поинты перезаписаны в Google Таблице'))
        finally:
            write_lock.release()
    except PermissionError as exc:
        return jsonify(error=str(exc)),403
    except (ValueError,SheetWriteError) as exc:
        return jsonify(error=str(exc)),409
    except Exception:
        logger.exception('Manual Google write failed')
        return jsonify(error='Ошибка ручной записи. Проверь Google Таблицу и журнал.'),500


def auto_write_loop():
    """Immediately react to new bot messages; retry connection failures periodically."""
    while True:
        auto_write_event.wait(timeout=60)
        auto_write_event.clear()
        cfg=auto_settings()
        if not cfg.get('enabled'):
            continue
        try:
            sid,gid=parse_sheet_link(get_default_sheet_url())
            if (sid,str(gid))!=(cfg.get('sid'),str(cfg.get('gid'))):
                auto_state['last_error']='Ссылка изменилась. Повтори настройку автообновления.'
                continue
            execute_write(cfg['part_col'],cfg['points_col'],cfg['first_row'],cfg['mode'])
        except Exception as exc:
            logger.warning('Google auto-write postponed: %s',exc)


@app.route('/api/sheet-write-status', methods=['GET'])
def sheet_write_status():
    return jsonify(configured=bool(get_service_email()), service_email=get_service_email(),
                   auto=auto_settings(),auto_state=auto_state)


@app.route('/api/auto-write-settings', methods=['GET','POST'])
def auto_write_settings_route():
    if request.method=='GET':
        return jsonify(**auto_settings(), state=auto_state)
    try:
        only_local_write()
        payload=request.get_json(silent=True) or {}
        if type(payload.get('enabled')) is not bool:
            raise ValueError('Укажи, включено ли автообновление')
        if not payload['enabled']:
            old=auto_settings();old['enabled']=False;write_settings(old)
            return jsonify(**old)
        pc,vc,first,mode=extract_options(payload)
        sid,gid=parse_sheet_link(get_default_sheet_url())
        with authorize() as session:
            snapshot=read_snapshot(session,sid,gid)
            build_write_preview(snapshot,pc,vc,first,{}, {})
        cfg=dict(enabled=True,sid=sid,gid=gid,part_col=pc,points_col=vc,first_row=first,mode=mode)
        write_settings(cfg)
        auto_write_event.set()
        return jsonify(**cfg)
    except PermissionError as exc:
        return jsonify(error=str(exc)),403
    except (ValueError,SheetWriteError) as exc:
        return jsonify(error=str(exc)),400


@app.route('/api/prepare-write', methods=['POST'])
def prepare_write():
    try:
        only_local_write()
        payload=request.get_json(silent=True) or {}
        pc,vc,first,mode=extract_options(payload)
        sid,gid=parse_sheet_link(get_default_sheet_url())
        with authorize() as session:
            reconcile_job(session,sid,gid)
            snapshot=read_snapshot(session,sid,gid)
        start,end=requested_period(payload)
        telegram=pending_telegram_points(sid,gid,mode,start,end)
        blocked=sync_point_reviews(snapshot,pc,vc,first,telegram,sid,gid,mode,start,end)
        preview=label_processed_rows(build_write_preview(snapshot,pc,vc,first,telegram,{},blocked),sid,gid)
        token=secrets.token_urlsafe(30)
        with write_lock:
            prepared_writes.clear()
            prepared_writes[token]=dict(created=time.time(),sid=sid,gid=gid,
                fingerprint=snapshot_fingerprint(snapshot), evidence=evidence_fingerprint(telegram),
                options=(pc,vc,first,mode,start,end))
        safe={key:value for key,value in preview.items() if key not in ('updates','outcomes')}
        return jsonify(**safe,plan_token=token,has_changes=bool(preview['updates']))
    except PermissionError as exc:
        return jsonify(error=str(exc)),403
    except (ValueError,SheetWriteError) as exc:
        return jsonify(error=str(exc)),400
    except Exception as exc:
        logger.exception('Prepare Google Sheets write failed')
        return jsonify(error='Не удалось подготовить запись: '+str(exc)[:180]),500


@app.route('/api/apply-write', methods=['POST'])
def apply_write():
    try:
        only_local_write()
        payload=request.get_json(silent=True) or {}
        token=payload.get('plan_token')
        plan=prepared_writes.get(token) if isinstance(token,str) else None
        if not plan or time.time()-plan['created']>PREVIEW_SECONDS:
            return jsonify(error='Предпросмотр устарел. Повтори сравнение.'),409
        if parse_sheet_link(get_default_sheet_url())!=(plan['sid'],plan['gid']):
            return jsonify(error='Основная таблица изменилась. Повтори сравнение.'),409
        pc,vc,first,mode,start,end=plan['options']
        with authorize() as session:
            fresh=read_snapshot(session,plan['sid'],plan['gid'])
        if snapshot_fingerprint(fresh)!=plan['fingerprint']:
            return jsonify(error='Google Таблица была изменена после сравнения. Повтори сравнение.'),409
        telegram=pending_telegram_points(plan['sid'],plan['gid'],mode,start,end)
        if evidence_fingerprint(telegram)!=plan['evidence']:
            return jsonify(error='Пришли новые сообщения Telegram. Повтори сравнение.'),409
        result=execute_write(pc,vc,first,mode,period=(start,end),expected_evidence=plan['evidence'])
        prepared_writes.pop(token,None)
        return jsonify(**result)
    except PermissionError as exc:
        return jsonify(error=str(exc)),403
    except (ValueError,SheetWriteError) as exc:
        return jsonify(error=str(exc)),409
    except Exception as exc:
        logger.exception('Write failed')
        return jsonify(error='Ошибка записи: '+str(exc)[:180]),500


def approve_point_increase(payload):
    """Explicitly approve one exact Google/Telegram comparison and no other part."""
    sid=payload.get('spreadsheet_id')
    gid=str(payload.get('gid'))
    part=payload.get('part')
    signature=payload.get('signature')
    if not all(isinstance(v,str) and v for v in (sid,part,signature)):
        raise ValueError('Обнови список проверки')
    if (sid,gid)!=parse_sheet_link(get_default_sheet_url()):
        raise ValueError('Основная Google Таблица изменилась. Обнови проверку.')
    with connect() as c:
        row=c.execute('SELECT * FROM sheet_point_reviews WHERE spreadsheet_id=? AND gid=? AND part=?',
                      (sid,gid,part)).fetchone()
    if not row or row['status']!='pending' or row['signature']!=signature:
        raise ValueError('Проверка изменилась. Обнови список.')
    pc,vc,first=row['part_col'],row['points_col'],row['first_row']
    mode,start,end=row['mode'],row['start_stamp'],row['end_stamp']
    pending=pending_telegram_points(sid,gid,mode,start,end)
    info=pending.get(part)
    if not info or info['signature']!=signature or info['points']!=row['new_value']:
        raise ValueError('Данные Telegram изменились. Повтори сравнение.')
    with authorize() as session:
        snapshot=read_snapshot(session,sid,gid)
    old=None
    for source in snapshot['rows'][first-1:]:
        if normalize_table_part(sheet_cell(source,pc))==part:
            old=sheet_cell(source,vc)
            break
    if old!=row['old_value'] or not excess_increase(old,info['points']):
        raise ValueError('Значение Google изменилось. Повтори сравнение.')
    with connect() as c:
        c.execute('UPDATE sheet_point_reviews SET status=? WHERE spreadsheet_id=? AND gid=? AND part=? AND signature=?',
                  ('approved',sid,gid,part,signature))
    try:
        result=execute_write(pc,vc,first,mode,only_parts={part},period=(start,end))
    except Exception as exc:
        auto_write_event.set()
        return jsonify(approved=True,written=False,message='Одобрено, но Google пока не обновлён: '+str(exc)[:200])
    auto_write_event.set()
    return jsonify(approved=True,written=True,message='Увеличение одобрено. Google Таблица обновлена.',result=result)


@app.route('/api/approve-review', methods=['POST'])
def approve_review():
    """Accept one explicit value; make it a normal report and try Google immediately."""
    try:
        only_local_write()
        payload=request.get_json(silent=True) or {}
        if payload.get('kind')=='sheet_points':
            return approve_point_increase(payload)
        chat_id,message_id=payload.get('chat_id'),payload.get('message_id')
        value=payload.get('points')
        if type(chat_id) is not int or type(message_id) is not int:
            raise ValueError('Неверный ID Telegram-сообщения')
        if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1000:
            raise ValueError('Введи поинты числом от 0 до 1000')
        if not isinstance(payload.get('text_hash'),str):
            raise ValueError('Обнови список сообщений')
        with connect() as c:
            row=c.execute('SELECT * FROM review_messages WHERE chat_id=? AND message_id=?', (chat_id,message_id)).fetchone()
            if not row:
                return jsonify(error='Сообщение уже одобрено или удалено. Обнови список.'),409
            if row['status']=='missing_id':
                return jsonify(error='Отсутствует ID сборки. Исправьте исходное сообщение в Telegram.'),409
            if not matches_scope(chat_id,row['thread_id']):
                return jsonify(error='Сообщение не принадлежит настроенной теме Telegram'),403
            digest=hashlib.sha256(row['text'].encode('utf-8')).hexdigest()
            if digest != payload['text_hash']:
                return jsonify(error='Сообщение изменилось. Обнови список.'),409
            c.execute('INSERT INTO approved_reports(chat_id,message_id,part,text_hash,approved_points,approved_utc) '
                'VALUES (?,?,?,?,?,?) ON CONFLICT(chat_id,message_id) DO UPDATE SET '
                'part=excluded.part,text_hash=excluded.text_hash,approved_points=excluded.approved_points,approved_utc=excluded.approved_utc',
                (chat_id,message_id,row['part'],digest,value,int(time.time())))
            c.execute('INSERT INTO messages(chat_id,message_id,thread_id,date_utc,part,points,text,assembly_id) VALUES (?,?,?,?,?,?,?,?) '
                'ON CONFLICT(chat_id,message_id,part) DO UPDATE SET points=excluded.points,text=excluded.text,assembly_id=excluded.assembly_id',
                (chat_id,message_id,row['thread_id'],row['date_utc'],row['part'],value,row['text'],row['assembly_id']))
            c.execute('DELETE FROM review_messages WHERE chat_id=? AND message_id=?',(chat_id,message_id))
        cfg=auto_settings()
        if not cfg.get('part_col') is None and get_service_email() and cfg.get('sid'):
            try:
                sid,gid=parse_sheet_link(get_default_sheet_url())
                if (sid,str(gid))!=(cfg['sid'],str(cfg['gid'])):
                    raise SheetWriteError('Таблица изменилась. Настрой запись заново.')
                result=execute_write(cfg['part_col'],cfg['points_col'],cfg['first_row'],cfg['mode'],
                                     {(chat_id,message_id)})
                auto_write_event.set()
                return jsonify(approved=True,written=True,message='Одобрено и записано в Google Таблицу',result=result)
            except Exception as exc:
                auto_write_event.set()
                return jsonify(approved=True,written=False,message='Отчёт одобрен и сохранён, но Google пока не обновлён: '+str(exc)[:200])
        auto_write_event.set()
        return jsonify(approved=True,written=False,message='Отчёт одобрен. Настрой автообновление для записи в Google.')
    except PermissionError as exc:
        return jsonify(error=str(exc)),403
    except (ValueError,SheetWriteError) as exc:
        return jsonify(error=str(exc)),400


if __name__ == '__main__':
    init_db()
    if TOKEN:
        try:
            me = api('getMe')
            state['bot_name'] = me.get('username')
            logger.info('Connected to @%s', state['bot_name'])
        except Exception as e: logger.warning('getMe failed: %s', e)
    threading.Thread(target=poll, daemon=True).start()
    threading.Thread(target=auto_write_loop, daemon=True, name="google-auto-writer").start()
    app.run(host=os.getenv('APP_HOST', '0.0.0.0' if os.getenv('RENDER') else '127.0.0.1'), port=APP_PORT, debug=False, use_reloader=False)
