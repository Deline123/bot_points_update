"""Google Sheets V4 writer: service account, existing-cell updates + new rows.

The service-account key is local and should never be included in distributed ZIPs.
"""
import os
from pathlib import Path

import requests

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
ROOT = Path(__file__).resolve().parent
BASE = 'https://sheets.googleapis.com/v4/spreadsheets'


class SheetWriteError(Exception):
    pass


def credential_path():
    """Use explicitly configured key or the private Bot Manager credential store.

    Secrets are never shipped with GitHub Releases. Existing in-app keys remain
    supported for old installations; Bot Manager's private store survives updates.
    """
    raw = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', 'google_service_account.json').strip()
    candidates = []
    if raw:
        configured = Path(raw).expanduser()
        candidates.append(configured if configured.is_absolute() else ROOT / configured)
    candidates.extend((ROOT.parent / 'secrets' / 'google_service_account.json',
                       ROOT / 'google_service_account.json'))
    seen = set()
    for path in candidates:
        path_key = str(path)
        if path_key in seen:
            continue
        seen.add(path_key)
        if path.is_file():
            return path
    return candidates[0] if candidates else None


def _service_account_info():
    """Render-compatible secret: JSON stored in an environment variable."""
    import json
    raw = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON', '').strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not value.get('client_email') or not value.get('private_key'):
            raise ValueError('incomplete service account')
        return value
    except ValueError as exc:
        raise SheetWriteError('GOOGLE_SERVICE_ACCOUNT_JSON contains invalid credentials') from exc


def get_service_email():
    info = _service_account_info()
    if info:
        return info.get('client_email')
    p = credential_path()
    if not p or not p.is_file():
        return None
    try:
        import json
        return json.loads(p.read_text(encoding='utf-8')).get('client_email')
    except (OSError, ValueError):
        return None


def authorize():
    info = _service_account_info()
    p = credential_path() if not info else None
    if not info and (not p or not p.is_file()):
        raise SheetWriteError('Настрой GOOGLE_SERVICE_ACCOUNT_JSON или GOOGLE_SERVICE_ACCOUNT_FILE.')
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        credentials = (service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
                       if info else service_account.Credentials.from_service_account_file(str(p), scopes=SCOPES))
        return AuthorizedSession(credentials)
    except ImportError as exc:
        raise SheetWriteError('Не установлен google-auth. Запусти start_bot_point.bat для установки зависимостей.') from exc
    except (ValueError, OSError) as exc:
        raise SheetWriteError('Неверный JSON ключа Google: ' + str(exc)[:180]) from exc


def api_request(session, method, url, **kwargs):
    try:
        response = session.request(method, url, timeout=35, **kwargs)
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else '?'
        details = ''
        if exc.response is not None:
            try:
                details = exc.response.json().get('error', {}).get('message', '')
            except (ValueError, AttributeError):
                pass
        if code in (401, 403):
            raise SheetWriteError('Google запретил доступ (HTTP ' + str(code) + '). Поделись таблицей с адресом сервисного аккаунта как «Редактор». ' + details[:160]) from exc
        raise SheetWriteError('Ошибка Google Sheets API (HTTP ' + str(code) + '): ' + details[:220]) from exc
    except requests.RequestException as exc:
        raise SheetWriteError('Ошибка соединения с Google Sheets: ' + str(exc)[:150]) from exc
    except (ValueError, AttributeError) as exc:
        raise SheetWriteError('Google вернул некорректный ответ') from exc


def sheet_title(session, spreadsheet_id, gid):
    data = api_request(session, 'GET', f'{BASE}/{spreadsheet_id}', params={
        'fields': 'sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))',
    })
    for entry in data.get('sheets', []):
        props = entry['properties']
        if str(props['sheetId']) == str(gid):
            return props['title'], props.get('gridProperties', {}).get('rowCount', 1000)
    raise SheetWriteError('Вкладка gid=' + str(gid) + ' не найдена. Проверь ссылку на лист.')


def quoted_title(title):
    return "'" + title.replace("'", "''") + "'"


def index_to_col(index):
    result = ''
    index += 1
    while index:
        index, rem = divmod(index-1, 26)
        result = chr(65+rem) + result
    return result


def read_snapshot(session, spreadsheet_id, gid):
    title, row_count = sheet_title(session, spreadsheet_id, gid)
    response = api_request(session, 'GET', f'{BASE}/{spreadsheet_id}/values/{requests.utils.quote(quoted_title(title), safe="")}',
        params={'valueRenderOption':'FORMATTED_VALUE'})
    rows = response.get('values', [])
    if len(rows) > 10000 or sum(len(str(v)) for r in rows for v in r) > 5000000:
        raise SheetWriteError('Таблица слишком большая (ограничение: 10 000 строк, 5 МБ)')
    if any(len(r) > 100 for r in rows):
        raise SheetWriteError('В таблице больше 100 столбцов')
    # Extra read detects formulas in the points column, which must not be overwritten.
    formula_response = api_request(session, 'GET', f'{BASE}/{spreadsheet_id}/values/{requests.utils.quote(quoted_title(title), safe="")}',
        params={'valueRenderOption':'FORMULA'})
    formulas = formula_response.get('values', [])
    return {'title': title, 'grid_rows': row_count, 'rows': rows, 'formulas': formulas,
            'gid':str(gid), 'spreadsheet_id':spreadsheet_id}


def apply_changes(session, snapshot, cells):
    """Cells = [(row_number, zero_based_column, value), ...]. RAW ensures part codes are text."""
    if not cells:
        return {'totalUpdatedCells': 0}
    if len(cells) > 4000:
        raise SheetWriteError('Слишком много изменённых ячеек для одной записи (>4000)')
    needed = max(row for row, _, _ in cells)
    if needed > 10000:
        raise SheetWriteError('Превышено ограничение 10 000 строк')
    if needed > snapshot['grid_rows']:
        api_request(session, 'POST', f"{BASE}/{snapshot['spreadsheet_id']}:batchUpdate", json={
            'requests': [{'appendDimension': {
                'sheetId': int(snapshot['gid']), 'dimension': 'ROWS',
                'length': needed - snapshot['grid_rows'],
            }}]
        })
    prefix = quoted_title(snapshot['title'])
    ranges = [{'range': f'{prefix}!{index_to_col(column)}{row}', 'values': [[value]]}
              for row, column, value in cells]
    return api_request(session, 'POST', f"{BASE}/{snapshot['spreadsheet_id']}/values:batchUpdate",
        json={'valueInputOption':'RAW', 'data': ranges})
