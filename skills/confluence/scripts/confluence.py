#!/usr/bin/env python3
"""Read and edit one Confluence Cloud page, given its URL.

    confluence.py doctor [--offline]                 dependencies + credentials; run before first use
    confluence.py check                              credentials present and accepted?
    confluence.py info URL                           title, id, version, space, parent
    confluence.py pull URL [-o FILE.md]              page -> Markdown (+ FILE.md.confluence.json)
    confluence.py push FILE.md [--dry-run] [--force] Markdown -> the page it was pulled from
    confluence.py get-storage URL [-o FILE.xml]      raw page source, for edits Markdown cannot carry
    confluence.py put-storage FILE.xml [--dry-run] [--force]
    confluence.py create --parent URL FILE.md [--dry-run]      new child page (ask the user first)
    confluence.py delete URL --confirm-title TITLE             move a page to the trash (ask first)
    confluence.py plantuml decode DATA | encode FILE.puml      the PlantUML app's data field

Pages are only touched through an explicit URL. push and put-storage refuse when
the page changed since it was pulled (another editor, the user in the browser):
pull again, merge, then push; --force only after the user agreed to overwrite.

Credentials: ~/.confluence/confluence.env (override with CONFLUENCE_ENV), or the
environment variables of the same names:

    CONFLUENCE_BASE_URL=https://<site>.atlassian.net/wiki
    CONFLUENCE_EMAIL=<email of the Atlassian account that owns the token>
    CONFLUENCE_API_TOKEN=<API token>

Exit codes: 0 ok, 1 refused or failed, 2 usage, 3 credentials missing or rejected,
4 a dependency is missing (see install_deps.sh).
"""

import argparse
import base64
import datetime
import difflib
import html
import html.entities
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zlib

try:
    import markdown
except ImportError:  # only the Markdown commands need it
    markdown = None
    _venv = pathlib.Path(__file__).resolve().parent.parent / '.venv'
    # Compare prefixes: the venv's python is a symlink to the same interpreter binary.
    if (_venv / 'bin' / 'python').is_file() and pathlib.Path(sys.prefix).resolve() != _venv.resolve():
        os.execv(str(_venv / 'bin' / 'python'), [str(_venv / 'bin' / 'python'), *sys.argv])

EXIT_REFUSED, EXIT_USAGE, EXIT_CREDENTIALS, EXIT_DEPENDENCY = 1, 2, 3, 4
SKILL_DIR = pathlib.Path(__file__).resolve().parent.parent
ENV_FILE = pathlib.Path(os.environ.get('CONFLUENCE_ENV',
                                       pathlib.Path.home() / '.confluence' / 'confluence.env'))
TOKEN_URL = 'https://id.atlassian.com/manage-profile/security/api-tokens'
SIDE_SUFFIX = '.confluence.json'
VERSION_MESSAGE = 'edited with confluence.py'

# Content rules for pages (see SKILL.md).
FORBIDDEN_ON_PAGES = ('github.com',)
ASCII_BOX_RE = re.compile(r'\+-{2,}')
ASCII_ARROW_RE = re.compile(r'-->|<--')
TREE_RE = re.compile(r'[├└]──')

# The "PlantUML Diagram" Forge app (format: references/plantuml-app.md).
PLANTUML_APP_SUFFIX = '/static/plantumlcloud'
PLANTUML_TITLE = 'PlantUML Diagram'

CODE_LANGUAGES = {'bash': 'bash', 'sh': 'bash', 'shell': 'bash', 'powershell': 'powershell',
                  'cpp': 'cpp', 'c++': 'cpp', 'c': 'cpp', 'python': 'python', 'py': 'python',
                  'yaml': 'yaml', 'json': 'json', 'xml': 'xml', 'java': 'java', 'js': 'js',
                  'javascript': 'js', 'sql': 'sql', 'text': 'none'}
ADMONITIONS = {'info': 'INFO', 'note': 'NOTE', 'warning': 'WARNING', 'tip': 'TIP'}


def fail(message, code=EXIT_REFUSED):
    print(f'confluence.py: {message}', file=sys.stderr)
    sys.exit(code)


def need_markdown():
    if markdown is None:
        fail('the Python package "markdown" is missing; run scripts/install_deps.sh '
             '(or: pip install markdown)', EXIT_DEPENDENCY)


# ---- credentials and REST ----------------------------------------------------------

def load_credentials():
    values = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, value = line.partition('=')
                values[key.strip()] = value.strip().strip('"\'')
    keys = ('CONFLUENCE_BASE_URL', 'CONFLUENCE_EMAIL', 'CONFLUENCE_API_TOKEN')
    creds = {k: os.environ.get(k) or values.get(k) for k in keys}
    missing = [k for k in keys if not creds[k]]
    if missing:
        fail(f'missing {", ".join(missing)} in {ENV_FILE} (or the environment).\n'
             f'  Ask the user to create an API token at {TOKEN_URL} and put these lines in\n'
             f'  {ENV_FILE} (then: chmod 600 {ENV_FILE}):\n'
             '    CONFLUENCE_BASE_URL=https://<site>.atlassian.net/wiki\n'
             '    CONFLUENCE_EMAIL=<email of the account that owns the token>\n'
             '    CONFLUENCE_API_TOKEN=<token>', EXIT_CREDENTIALS)
    if ENV_FILE.is_file() and ENV_FILE.stat().st_mode & 0o077:
        print(f'warning: {ENV_FILE} is readable by other users; run: chmod 600 {ENV_FILE}',
              file=sys.stderr)
    creds['CONFLUENCE_BASE_URL'] = creds['CONFLUENCE_BASE_URL'].rstrip('/')
    return creds


class Confluence:
    def __init__(self, creds):
        self.base = creds['CONFLUENCE_BASE_URL']
        token = f'{creds["CONFLUENCE_EMAIL"]}:{creds["CONFLUENCE_API_TOKEN"]}'
        self.auth = 'Basic ' + base64.b64encode(token.encode()).decode()

    def request(self, method, path, payload=None, query=None, retries=3, absolute=False):
        url = (path if absolute else self.base + path)
        if query:
            url += '?' + urllib.parse.urlencode(query)
        data = json.dumps(payload).encode('utf-8') if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': self.auth, 'Accept': 'application/json',
            'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read() or b'{}')
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:400]
            if e.code in (401, 403):
                fail(f'{method} {path} -> HTTP {e.code}: the token was rejected or lacks access.\n'
                     f'  CONFLUENCE_EMAIL must be the account that created the token; the token\n'
                     f'  may be expired or revoked. Ask the user for a new one ({TOKEN_URL}).',
                     EXIT_CREDENTIALS)
            fail(f'{method} {path} -> HTTP {e.code}: {detail}')
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if retries <= 0:
                raise ConnectionError(f'{method} {path}: {e}') from e
            print(f'  network error ({e}), retrying ...', file=sys.stderr)
            time.sleep(3)
            return self.request(method, path, payload, query, retries - 1, absolute)

    def page(self, page_id, version=None):
        query = {'body-format': 'storage'}
        if version:
            query['version'] = version
        return self.request('GET', f'/api/v2/pages/{page_id}', query=query)

    def update(self, page, title, storage, message=VERSION_MESSAGE):
        return self.request('PUT', f'/api/v2/pages/{page["id"]}', {
            'id': page['id'], 'status': 'current', 'title': title,
            'body': {'representation': 'storage', 'value': storage},
            'version': {'number': page['version']['number'] + 1, 'message': message}}, retries=0)

    def create(self, space_id, parent_id, title, storage):
        return self.request('POST', '/api/v2/pages', {
            'spaceId': space_id, 'status': 'current', 'title': title, 'parentId': parent_id,
            'body': {'representation': 'storage', 'value': storage}}, retries=0)

    def find(self, space_id, title):
        found = self.request('GET', '/api/v2/pages',
                             query={'space-id': space_id, 'title': title, 'status': 'current'})
        return (found.get('results') or [None])[0]

    def space_pages(self, space_id, limit=100):
        found = self.request('GET', '/api/v2/pages', query={
            'space-id': space_id, 'status': 'current', 'body-format': 'storage', 'limit': limit})
        return found.get('results', [])

    def space(self, space_id):
        return self.request('GET', f'/api/v2/spaces/{space_id}')

    def web_url(self, page):
        return self.base + ((page.get('_links') or {}).get('webui') or f'/pages/{page["id"]}')


def connect():
    return Confluence(load_credentials())


def page_storage(page):
    return ((page.get('body') or {}).get('storage') or {}).get('value') or ''


def page_id_from_url(url, base):
    """Accepts .../pages/<id>/..., ?pageId=<id>, /wiki/x/<tiny link>, or a bare id."""
    if re.fullmatch(r'\d+', url.strip()):
        return url.strip()
    parsed = urllib.parse.urlparse(url.strip())
    site = urllib.parse.urlparse(base)
    if parsed.netloc and parsed.netloc != site.netloc:
        fail(f'{url} is on {parsed.netloc}, but the credentials are for {site.netloc}')
    match = re.search(r'/pages/(?:edit-v2/|edit/)?(\d+)', parsed.path)
    if match:
        return match.group(1)
    query = urllib.parse.parse_qs(parsed.query)
    if query.get('pageId'):
        return query['pageId'][0]
    match = re.search(r'/x/([A-Za-z0-9_-]+)', parsed.path)
    if match:  # tiny link: base64url of the little-endian page id
        tiny = match.group(1)
        raw = base64.urlsafe_b64decode(tiny + '=' * (-len(tiny) % 4))
        return str(int.from_bytes(raw, 'little'))
    fail(f'cannot find a page id in {url}; give the page link from the browser', EXIT_USAGE)


# ---- sidecar: which page a local file belongs to, at which version ------------------

def sidecar_path(local):
    return pathlib.Path(str(local) + SIDE_SUFFIX)


def write_sidecar(local, api, page):
    sidecar_path(local).write_text(json.dumps({
        'base': api.base, 'id': str(page['id']), 'title': page['title'],
        'version': page['version']['number'], 'space_id': str(page.get('spaceId', '')),
        'pulled_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
    }, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def read_sidecar(local):
    path = sidecar_path(local)
    if not path.is_file():
        fail(f'{path} not found: pull the page first (push only writes back to the page it '
             'was pulled from)', EXIT_USAGE)
    return json.loads(path.read_text(encoding='utf-8'))


def guarded_page(api, side, force):
    """The current page, if nobody changed it since it was pulled."""
    if side['base'] != api.base:
        fail(f'the file was pulled from {side["base"]}, the credentials are for {api.base}')
    page = api.page(side['id'])
    current = page['version']['number']
    if current != side['version'] and not force:
        fail(f'"{page["title"]}" changed since it was pulled (version {side["version"]} -> '
             f'{current}); pull it again and merge. Use --force only if the user agreed to '
             'overwrite those changes.')
    return page


def show_diff(old, new, old_name, new_name, limit=60):
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), old_name, new_name,
                                     lineterm=''))
    for line in diff[:limit]:
        print('    ' + line)
    if len(diff) > limit:
        print(f'    ... {len(diff) - limit} more lines')
    if not diff:
        print('    (no changes)')


# ---- storage parsing ------------------------------------------------------------------

AC = '{urn:ac}'
RI = '{urn:ri}'


def parse_storage(storage):
    """Storage XHTML -> element tree. Named HTML entities (&nbsp; ...) are not valid XML."""
    def numeric(match):
        name = match.group(1)
        if name in ('amp', 'lt', 'gt', 'quot', 'apos'):
            return match.group(0)
        code = html.entities.name2codepoint.get(name)
        return f'&#{code};' if code is not None else match.group(0)

    parts = re.split(r'(<!\[CDATA\[.*?\]\]>)', storage, flags=re.S)
    parts[::2] = [re.sub(r'&([A-Za-z][A-Za-z0-9]*);', numeric, part) for part in parts[::2]]
    return ET.fromstring('<root xmlns:ac="urn:ac" xmlns:ri="urn:ri" xmlns:at="urn:at">' +
                         ''.join(parts) + '</root>')


def check_page_rules(storage, what):
    """Well-formed, no links into code hosting, no ASCII-art diagrams."""
    try:
        parse_storage(storage)
    except ET.ParseError as e:
        fail(f'{what}: not well-formed storage XHTML: {e}')
    for word in FORBIDDEN_ON_PAGES:
        hits = [m.group(0) for m in re.finditer(r'.{0,40}' + re.escape(word) + r'.{0,40}', storage)]
        if hits:
            fail(f'{what}: pages must not reference {word}: ' + ' | '.join(hits[:3]))
    for match in CODE_MACRO_RE.finditer(storage):
        code = html.unescape(macro_code(match.group(0)))
        if macro_language(match.group(0)) == 'none' and is_ascii_diagram(code):
            first = next((line.strip() for line in code.split('\n') if line.strip()), '')
            fail(f'{what}: ASCII diagram in a code block ("{first[:50]}"): draw it with PlantUML '
                 '(a ```plantuml block in Markdown)')


def is_ascii_diagram(code):
    return bool(ASCII_BOX_RE.search(code) or (ASCII_ARROW_RE.search(code) and not TREE_RE.search(code)))


CODE_MACRO_RE = re.compile(r'<ac:structured-macro\b[^>]*\bac:name="code"[^>]*>.*?</ac:structured-macro>',
                           flags=re.S)
ADF_EXTENSION_RE = re.compile(r'<ac:adf-extension>.*?</ac:adf-extension>', flags=re.S)


def macro_code(macro_xml):
    body = re.search(r'<ac:plain-text-body>(.*?)</ac:plain-text-body>', macro_xml, flags=re.S)
    return ''.join(re.findall(r'<!\[CDATA\[(.*?)\]\]>', body.group(1), flags=re.S)) if body else ''


def macro_language(macro_xml):
    match = re.search(r'<ac:parameter ac:name="language">([^<]*)</ac:parameter>', macro_xml)
    return match.group(1) if match else 'none'


# ---- PlantUML app ------------------------------------------------------------------

def plantuml_encode(source):
    """Raw deflate (level 9), base64: what the app stores in guest-params/data."""
    deflate = zlib.compressobj(9, zlib.DEFLATED, -15)
    return base64.b64encode(deflate.compress(source.encode('utf-8')) + deflate.flush()).decode('ascii')


def plantuml_decode(data, compressed=True):
    if not compressed:
        return data
    return zlib.decompress(base64.b64decode(data), -15).decode('utf-8')


def extension_fields(el):
    """(extension key, title, guest-params dict) of an <ac:adf-extension> element."""
    node = el.find(AC + 'adf-node')
    if node is None:
        return '', '', {}
    attrs = {a.get('key'): a for a in node.findall(AC + 'adf-attribute')}
    key = (attrs['extension-key'].text or '') if 'extension-key' in attrs else ''
    title, guest = '', {}
    params = attrs.get('parameters')
    if params is not None:
        for param in params.findall(AC + 'adf-parameter'):
            if param.get('key') == 'guest-params':
                guest = {g.get('key'): g for g in param.findall(AC + 'adf-parameter')}
            elif param.get('key') == 'extension-title':
                title = param.text or ''
    return key, title, guest


def extension_source(el):
    """PlantUML source of a "PlantUML Diagram" macro, or None for any other app macro."""
    key, _, guest = extension_fields(el)
    if not key.endswith(PLANTUML_APP_SUFFIX) or 'data' not in guest:
        return None
    compressed = 'compressed' in guest and (guest['compressed'].text or '') == 'true'
    return plantuml_decode(guest['data'].text or '', compressed).rstrip('\n')


class DiagramContext:
    """What the editor stores with a diagram: the app, the page, the site. Copied from an
    existing diagram in the space, because the workspace id is not exposed by the REST API."""

    def __init__(self, app_key, page_id, page_version, space_key, space_id, cloud_id, account_id,
                 context_id):
        self.app_key, self.page_id, self.page_version = app_key, str(page_id), page_version
        self.space_key, self.space_id = space_key, str(space_id)
        self.cloud_id, self.account_id, self.context_id = cloud_id, account_id, context_id


def context_from_macro(macro_xml, page_id, page_version):
    el = parse_storage(macro_xml)[0]
    key, _, _ = extension_fields(el)
    values = {}
    for param in el.iter(AC + 'adf-parameter'):
        values.setdefault(param.get('key'), param)
    space = values.get('space')
    space_fields = {c.get('key'): (c.text or '') for c in space} if space is not None else {}
    ids = values.get('context-ids')
    value = ids.find(AC + 'adf-parameter-value') if ids is not None else None
    text = lambda k: (values[k].text or '') if k in values else ''
    return DiagramContext(key, page_id, page_version, space_fields.get('key', ''),
                          space_fields.get('id', ''), text('cloud-id'), text('account-id'),
                          (value.text or '') if value is not None else '')


def find_diagram_context(api, page, page_version):
    """From a diagram on this page, else from any page in the same space."""
    candidates = [page_storage(page)]
    candidates += [page_storage(p) for p in api.space_pages(page['spaceId'])]
    for storage in candidates:
        for match in ADF_EXTENSION_RE.finditer(storage):
            if PLANTUML_APP_SUFFIX in match.group(0) and 'embedded-macro-context' in match.group(0):
                return context_from_macro(match.group(0), page['id'], page_version)
    fail(f'no "{PLANTUML_TITLE}" diagram exists in this space yet, so its site context is unknown. '
         'Ask the user to insert one diagram by hand (any page, any content), then run again.')


def adf_param(key, value, kind=None):
    kind_attr = f' type="{kind}"' if kind else ''
    return f'<ac:adf-parameter key="{key}"{kind_attr}>{value}</ac:adf-parameter>'


def plantuml_macro(source, name, ctx, index):
    esc = lambda text: html.escape(str(text), quote=False)
    local_id = uuid.uuid5(uuid.NAMESPACE_URL, f'confluence.py/{ctx.page_id}/{index}/{source}')
    guest_id = uuid.uuid5(uuid.NAMESPACE_URL, f'confluence.py/{ctx.page_id}/{index}/{source}/diagram')
    data = plantuml_encode(source)
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    context = adf_param('embedded-macro-context', ''.join([
        adf_param('extension-data', ''.join([
            adf_param('type', 'macro'),
            adf_param('content', adf_param('id', esc(ctx.page_id)) + adf_param('type', 'page') +
                      adf_param('version', ctx.page_version, 'integer')),
            adf_param('space', adf_param('key', esc(ctx.space_key)) + adf_param('id', esc(ctx.space_id))),
        ])),
        adf_param('context-ids', f'<ac:adf-parameter-value>{esc(ctx.context_id)}</ac:adf-parameter-value>'),
        adf_param('account-id', esc(ctx.account_id)),
        adf_param('cloud-id', esc(ctx.cloud_id)),
    ]))
    guest = adf_param('guest-params', ''.join([
        adf_param('id', guest_id), adf_param('name', esc(name)), adf_param('data', data),
        adf_param('revision', 1, 'integer'), adf_param('compressed', 'true', 'boolean'),
        adf_param('history', '<ac:adf-parameter-value>' + adf_param('data', data) +
                  adf_param('revision', 1, 'integer') + adf_param('created-at', now) +
                  '</ac:adf-parameter-value>'),
        adf_param('draft', 'false', 'boolean'),
    ]))
    params = ''.join([
        adf_param('local-id', local_id),
        adf_param('extension-id', f'ari:cloud:ecosystem::extension/{ctx.app_key}'),
        adf_param('extension-title', PLANTUML_TITLE), adf_param('layout', 'extension'),
        adf_param('forge-environment', 'PRODUCTION'), adf_param('render', 'default'),
        context, guest])
    node = ('<ac:adf-node type="extension">'
            f'<ac:adf-attribute key="extension-key">{ctx.app_key}</ac:adf-attribute>'
            '<ac:adf-attribute key="extension-type">com.atlassian.ecosystem</ac:adf-attribute>'
            f'<ac:adf-attribute key="parameters">{params}</ac:adf-attribute>'
            f'<ac:adf-attribute key="text">{PLANTUML_TITLE}</ac:adf-attribute>'
            '<ac:adf-attribute key="layout">default</ac:adf-attribute>'
            f'<ac:adf-attribute key="local-id">{local_id}</ac:adf-attribute>'
            '</ac:adf-node>')
    return f'<ac:adf-extension>{node}<ac:adf-fallback>{node}</ac:adf-fallback></ac:adf-extension>'


def check_plantuml(sources):
    sources = list(sources)
    if not sources:
        return
    exe = shutil.which('plantuml')
    if not exe:
        fail('plantuml not found; run scripts/install_deps.sh (diagrams are syntax checked '
             'before they go on a page)', EXIT_DEPENDENCY)
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for index, source in enumerate(sources):
            path = pathlib.Path(tmp) / f'diagram-{index + 1}.puml'
            path.write_text(source + '\n', encoding='utf-8')
            files.append(str(path))
        result = subprocess.run([exe, '-checkonly', *files], capture_output=True, text=True)
        if result.returncode != 0:
            fail('PlantUML syntax check failed (diagrams numbered in page order):\n' +
                 result.stdout + result.stderr)


# ---- storage -> Markdown ---------------------------------------------------------------

BLOCK_TAGS = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'table', 'pre', 'hr',
              'blockquote', AC + 'structured-macro', AC + 'adf-extension'}


class ConversionError(Exception):
    """The page holds content Markdown cannot carry. Never drop content silently."""

    def __init__(self, problems):
        super().__init__('; '.join(problems))
        self.problems = problems


def escape_text(text, in_table=False):
    text = text.replace('\\', '\\\\')
    text = re.sub(r'([`*])', r'\\\1', text)
    text = re.sub(r'(?<!\w)_|_(?!\w)', r'\\_', text)
    text = re.sub(r'\](?=[(\[])', r'\\]', text)
    text = re.sub(r'&(?=#?\w+;)', '&amp;', text)
    text = re.sub(r'<(?=[A-Za-z/!?])', '&lt;', text)
    return text.replace('|', '\\|') if in_table else text


def escape_line_start(line):
    if re.match(r'=+\s*$', line):
        return '&#61;' + line[1:]
    if line.startswith('~~~'):
        return '&#126;' + line[1:]
    if re.match(r'#|>|[-+](\s|$)|-+\s*$|\[[^\]]*\]:', line):
        return '\\' + line
    return re.sub(r'^(\d+)\.(?=\s)', r'\1\\.', line)


def page_href(title):
    return 'page:' + urllib.parse.quote(title)


class MarkdownWriter:
    def __init__(self):
        self.problems = []
        self.in_table = False

    def problem(self, what, el=None):
        text = ' '.join(''.join(el.itertext()).split())[:40] if el is not None else ''
        self.problems.append(what + (f' near "{text}"' if text else ''))

    def blocks(self, parent):
        out = []
        if (parent.text or '').strip():
            self.problem('text outside a block', parent)
        for el in parent:
            if (el.tail or '').strip():
                self.problem('text outside a block', el)
            md = self.block(el)
            if md:
                out.append(md)
        return out

    def block(self, el):
        tag = el.tag
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            text = ' '.join(self.inline(el).split())
            return f'{"#" * int(tag[1])} {text}' if text else None
        if tag == 'p':
            text = self.inline(el).strip()
            return '\n'.join(escape_line_start(line) for line in text.split('\n')) if text else None
        if tag in ('ul', 'ol'):
            return self.list_block(el)
        if tag == 'table':
            return self.table(el)
        if tag == 'pre':
            return self.fence(''.join(el.itertext()).rstrip('\n'), '')
        if tag == 'hr':
            return '---'
        if tag == 'blockquote':
            return self.quote('\n\n'.join(self.blocks(el)))
        if tag == AC + 'structured-macro':
            return self.macro(el)
        if tag == AC + 'adf-extension':
            return self.extension(el)
        self.problem(f'unsupported block <{tag.split("}")[-1]}>', el)
        return None

    @staticmethod
    def quote(body):
        return '\n'.join(f'> {line}'.rstrip() for line in body.split('\n'))

    def macro(self, el):
        name = el.get(AC + 'name')
        params = {p.get(AC + 'name'): (p.text or '') for p in el.findall(AC + 'parameter')}
        if name == 'code':
            if params.get('title'):
                self.problem('code block with a title', el)
            body = el.find(AC + 'plain-text-body')
            code = (body.text or '') if body is not None else ''
            language = (params.get('language') or '').lower()
            language = '' if language == 'none' else language
            return self.fence(code.rstrip('\n'), language)
        if name in ADMONITIONS:
            if params.get('title'):
                self.problem(f'{name} panel with a title', el)
            body = el.find(AC + 'rich-text-body')
            inner = '\n\n'.join(self.blocks(body)) if body is not None else ''
            return self.quote(f'[!{ADMONITIONS[name]}]' + ('\n' + inner if inner else ''))
        self.problem(f'unsupported macro "{name}" (use get-storage / put-storage)', el)
        return None

    def extension(self, el):
        source = extension_source(el)
        if source is None:
            key, title, _ = extension_fields(el)
            self.problem(f'unsupported app macro "{title or key or "?"}" (use get-storage / put-storage)')
            return None
        return self.fence(source, 'plantuml')

    @staticmethod
    def fence(code, language):
        longest = max((len(run) for run in re.findall(r'`+', code)), default=0)
        ticks = '`' * max(3, longest + 1)
        return f'{ticks}{language}\n{code}\n{ticks}' if code else f'{ticks}{language}\n{ticks}'

    def flow(self, el):
        out = []
        run = ET.Element('p')
        run.text = el.text

        def flush():
            nonlocal run
            if (run.text or '').strip() or len(run):
                md = self.block(run)
                if md:
                    out.append((md, 'p'))
            run = ET.Element('p')

        for child in el:
            if child.tag in BLOCK_TAGS:
                flush()
                if child.tag in ('pre', 'table', AC + 'structured-macro', AC + 'adf-extension'):
                    self.problem('code block, table or macro inside a list', child)
                else:
                    md = self.block(child)
                    if md:
                        out.append((md, child.tag))
                run.text = child.tail
            else:
                run.append(child)
        flush()
        return out

    def list_block(self, el):
        ordered = el.tag == 'ol'
        number = int(el.get('start', '1'))
        items = []
        for li in el:
            if li.tag != 'li':
                self.problem(f'unexpected <{li.tag}> in a list', li)
                continue
            body = ''
            for index, (md, tag) in enumerate(self.flow(li)):
                joiner = '' if index == 0 else ('\n' if tag in ('ul', 'ol') else '\n\n')
                body += joiner + md
            marker = f'{number}. ' if ordered else '- '
            number += 1
            lines = body.split('\n')
            items.append('\n'.join([marker + lines[0]] + [f'    {l}' if l else '' for l in lines[1:]]))
        return '\n'.join(items) if items else None

    def table(self, el):
        rows = []
        for tr in el.iter('tr'):
            row = []
            for cell in tr:
                if cell.tag not in ('th', 'td'):
                    self.problem(f'unexpected <{cell.tag}> in a table row', cell)
                    continue
                if cell.get('colspan', '1') != '1' or cell.get('rowspan', '1') != '1':
                    self.problem('merged table cells', cell)
                row.append(self.cell(cell))
            rows.append(row)
        if not rows or any(len(row) != len(rows[0]) for row in rows):
            self.problem('table with uneven rows', el)
            return None
        lines = ['| ' + ' | '.join(row) + ' |' for row in rows]
        lines.insert(1, '|' + '|'.join('---' for _ in rows[0]) + '|')
        return '\n'.join(lines)

    def cell(self, cell):
        children = [c for c in cell if c.tag in BLOCK_TAGS]
        paragraphs = [c for c in children if c.tag == 'p' and ((c.text or '').strip() or len(c))]
        if len(children) != sum(c.tag == 'p' for c in children) or len(paragraphs) > 1:
            self.problem('more than one paragraph, or a block, in a table cell', cell)
            return ''
        self.in_table = True
        source = paragraphs[0] if paragraphs else cell
        text = ' '.join(self.inline(source).split('\n')).strip() if not (children and not paragraphs) else ''
        self.in_table = False
        return text

    def text(self, value):
        return escape_text(value, self.in_table) if value else ''

    def inline(self, el):
        out = [self.text(el.text)]
        for child in el:
            out.append(self.inline_element(child))
            out.append(self.text(child.tail))
        return ''.join(out)

    def wrap(self, el, mark):
        inner = self.inline(el)
        stripped = inner.strip()
        if not stripped:
            return inner
        lead = inner[:len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        return f'{lead}{mark}{stripped}{mark}{trail}'

    def code_span(self, el):
        code = ''.join(el.itertext())
        if not code:
            return ''
        if self.in_table and '|' in code:
            return '<code>' + escape_text(code).replace('|', '&#124;') + '</code>'
        longest = max((len(run) for run in re.findall(r'`+', code)), default=0)
        ticks = '`' * (longest + 1)
        pad = ' ' if code.startswith('`') or code.endswith('`') else ''
        return f'{ticks}{pad}{code}{pad}{ticks}'

    def inline_element(self, el):
        tag = el.tag
        if tag in ('strong', 'b'):
            return self.wrap(el, '**')
        if tag in ('em', 'i'):
            return self.wrap(el, '*')
        if tag == 'code':
            return self.code_span(el)
        if tag == 'br':
            return '<br />' if self.in_table else '  \n'
        if tag == 'a':
            href = el.get('href')
            if not href:
                return self.inline(el)
            return f'[{self.inline(el) or escape_text(href, self.in_table)}]({href})'
        if tag == AC + 'link':
            return self.page_link(el)
        if tag in ('span', AC + 'inline-comment-marker'):  # comments are not page content
            return self.inline(el)
        self.problem(f'unsupported inline <{tag.split("}")[-1]}> (use get-storage / put-storage)', el)
        return ''

    def page_link(self, el):
        page = el.find(RI + 'page')
        if page is None or page.get(RI + 'space-key'):
            self.problem('link to an attachment, user or another space (use get-storage)', el)
            return ''
        title = page.get(RI + 'content-title')
        body = el.find(AC + 'link-body')
        plain = el.find(AC + 'plain-text-link-body')
        if body is not None:
            text = self.inline(body)
        elif plain is not None:
            text = escape_text(plain.text or '', self.in_table)
        else:
            text = escape_text(title, self.in_table)
        anchor = el.get(AC + 'anchor')
        return f'[{text}]({page_href(title)}{"#" + urllib.parse.quote(anchor) if anchor else ""})'


def storage_to_markdown(title, storage):
    writer = MarkdownWriter()
    blocks = writer.blocks(parse_storage(storage))
    if writer.problems:
        raise ConversionError(writer.problems)
    return '\n\n'.join([f'# {title}'] + blocks) + '\n'


# ---- Markdown -> storage ---------------------------------------------------------------

def cdata(text):
    return '<![CDATA[' + text.replace(']]>', ']]]]><![CDATA[>') + ']]>'


def split_title(md_text, path):
    first, _, rest = md_text.partition('\n')
    if not first.startswith('# '):
        fail(f'{path}: the first line must be the page title as "# Title"', EXIT_USAGE)
    return first[2:].strip(), rest


def heading_before(storage, pos):
    found = None
    for match in re.finditer(r'<h[1-6][^>]*>(.*?)</h[1-6]>', storage[:pos], flags=re.S):
        found = match
    return ' '.join(html.unescape(re.sub(r'<[^>]+>', '', found.group(1))).split()) if found else ''


def to_storage(md_text, path, diagrams=None):
    """Markdown -> storage. diagrams(sources) -> list of macro XML for the ```plantuml blocks."""
    need_markdown()
    title, body_md = split_title(md_text, path)
    body = markdown.markdown(body_md, extensions=['tables', 'fenced_code'], output_format='xhtml')
    sources = []

    def code(match):
        lang = (match.group(1) or '').lower()
        text = html.unescape(match.group(2)).rstrip('\n')
        if lang == 'plantuml':
            sources.append(text)
            return f'<!--diagram-{len(sources) - 1}-->'
        return ('<ac:structured-macro ac:name="code">'
                f'<ac:parameter ac:name="language">{CODE_LANGUAGES.get(lang, "none")}</ac:parameter>'
                f'<ac:plain-text-body>{cdata(text)}</ac:plain-text-body></ac:structured-macro>')

    body = re.sub(r'<pre><code(?: class="language-([\w+-]+)")?>(.*?)</code></pre>', code, body, flags=re.S)
    body = re.sub(r'<a href="page:([^"#]*)(?:#([^"]*))?">(.*?)</a>', lambda m: (
        f'<ac:link{" ac:anchor=" + chr(34) + html.escape(urllib.parse.unquote(m.group(2)), quote=True) + chr(34) if m.group(2) else ""}>'
        f'<ri:page ri:content-title="{html.escape(urllib.parse.unquote(html.unescape(m.group(1))), quote=True)}" />'
        f'<ac:link-body>{m.group(3)}</ac:link-body></ac:link>'), body, flags=re.S)
    body = admonitions(body)
    if sources:
        names = [heading_before(body, body.index(f'<!--diagram-{i}-->')) for i in range(len(sources))]
        macros = diagrams(sources, names) if diagrams else [''] * len(sources)
        for i, macro in enumerate(macros):
            body = body.replace(f'<!--diagram-{i}-->', macro)
    return title, body, sources


def admonitions(body):
    """> [!INFO] ... blockquotes -> the info / note / warning / tip panels."""
    names = {v: k for k, v in ADMONITIONS.items()}

    def panel(match):
        inner = match.group(1)
        head = re.match(r'\s*<p>\[!(INFO|NOTE|WARNING|TIP)\](?:\s*\n|\s*</p>|\s*<br\s*/>)', inner)
        if not head:
            return match.group(0)
        rest = inner[head.end():]
        if not head.group(0).rstrip().endswith('</p>'):
            rest = '<p>' + rest
        rest = re.sub(r'^\s*<p>\s*</p>', '', rest)
        return (f'<ac:structured-macro ac:name="{names[head.group(1)]}"><ac:rich-text-body>'
                f'{rest.strip()}</ac:rich-text-body></ac:structured-macro>')

    return re.sub(r'<blockquote>(.*?)</blockquote>', panel, body, flags=re.S)


def revise_macro(macro_xml, source):
    """Create an app-style revision while preserving the macro ids and name."""
    data = plantuml_encode(source)
    now = (datetime.datetime.now(datetime.timezone.utc)
           .strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z')
    current = re.search(
        r'key="name">[^<]*</ac:adf-parameter><ac:adf-parameter key="data">[^<]*'
        r'</ac:adf-parameter><ac:adf-parameter key="revision" type="integer">(\d+)<',
        macro_xml,
    )
    if not current:
        return None
    revision = int(current.group(1)) + 1
    revised = re.sub(
        r'(key="name">[^<]*</ac:adf-parameter><ac:adf-parameter key="data">)[^<]*'
        r'(</ac:adf-parameter><ac:adf-parameter key="revision" type="integer">)\d+<',
        lambda m: f'{m.group(1)}{data}{m.group(2)}{revision}<',
        macro_xml,
    )
    entry = ('<ac:adf-parameter-value>' + adf_param('data', data) +
             adf_param('revision', revision, 'integer') + adf_param('created-at', now) +
             '</ac:adf-parameter-value>')
    revised = re.sub(
        r'(<ac:adf-parameter key="history">.*?)'
        r'(</ac:adf-parameter><ac:adf-parameter key="draft")',
        lambda m: m.group(1) + entry + m.group(2),
        revised,
        flags=re.S,
    )
    try:
        if extension_source(parse_storage(revised)[0]) != source:
            return None
    except ET.ParseError:
        return None
    return revised


def diagram_builder(api, page, next_version):
    """Keep unchanged diagrams and revise changed diagrams at their original positions."""
    existing, in_order = {}, []
    for match in ADF_EXTENSION_RE.finditer(page_storage(page) if page else ''):
        try:
            source = extension_source(parse_storage(match.group(0))[0])
        except ET.ParseError:
            continue
        if source is not None:
            existing.setdefault(source, match.group(0))
            in_order.append((source, match.group(0)))
    state = {}

    def build(sources, names):
        # Unchanged diagrams are already on the page and need no local syntax check.
        check_plantuml(s for s in sources if s not in existing)
        kept = set(sources)
        out = []
        for index, (source, name) in enumerate(zip(sources, names)):
            if source in existing:
                out.append(existing[source])
                continue
            if index < len(in_order) and in_order[index][0] not in kept:  # edited in place
                revised = revise_macro(in_order[index][1], source)
                if revised:
                    out.append(revised)
                    continue
            if 'ctx' not in state:
                state['ctx'] = find_diagram_context(api, page, next_version)
            out.append(plantuml_macro(source, name or f'diagram {index + 1}', state['ctx'], index))
        return out

    return build


# ---- commands --------------------------------------------------------------------------

def default_output(page, suffix):
    slug = re.sub(r'[^\w一-鿿-]+', '-', page['title']).strip('-')[:60] or 'page'
    return pathlib.Path(f'{page["id"]}-{slug}{suffix}')


def cmd_doctor(args):
    """Everything the skill needs, with the fix for each missing piece. Run before first use."""
    problems = 0

    def report(ok, what, fix=''):
        nonlocal problems
        print(f'{"ok     " if ok else "MISSING"} {what}' + ('' if ok else f'\n          fix: {fix}'))
        problems += 0 if ok else 1

    install = f'bash {SKILL_DIR}/scripts/install_deps.sh'
    report(sys.version_info >= (3, 8), f'python {sys.version.split()[0]}', 'install python3 (3.8 or newer)')
    report(markdown is not None, 'python package "markdown" (pull / push)',
           f'{install} --no-plantuml   (no sudo; the agent may run it)')
    java, dot, puml = shutil.which('java'), shutil.which('dot'), shutil.which('plantuml')
    version = ''
    if puml and java:
        result = subprocess.run([puml, '-version'], capture_output=True, text=True)
        version = (result.stdout.splitlines() or [''])[0]
    report(bool(java), 'java (runs plantuml)', f'{install}   (needs sudo: the user runs it)')
    report(bool(dot), 'graphviz "dot" (plantuml layout)', f'{install}   (needs sudo: the user runs it)')
    report(bool(version), f'plantuml {version} (diagram syntax check)'.replace('  ', ' '),
           f'{install}   (needs sudo: the user runs it)')
    report(bool(shutil.which('git')), 'git (optional: version the pulled files)', 'sudo apt-get install -y git')

    keys = ('CONFLUENCE_BASE_URL', 'CONFLUENCE_EMAIL', 'CONFLUENCE_API_TOKEN')
    values = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
            key, _, value = line.strip().partition('=')
            values[key.strip()] = value.strip().strip('"\'')
    report(ENV_FILE.is_file(), f'credentials file {ENV_FILE}',
           f'ask the user to create it with {", ".join(keys)}; token from {TOKEN_URL}')
    for key in keys:
        present = bool(os.environ.get(key) or values.get(key))
        report(present, f'{key} is set', f'ask the user to add {key} to {ENV_FILE}' +
               (f' (create the token at {TOKEN_URL})' if key.endswith('TOKEN') else ''))
    if ENV_FILE.is_file():
        report(not ENV_FILE.stat().st_mode & 0o077, f'{ENV_FILE} is private (600)', f'chmod 600 {ENV_FILE}')
    if not args.offline and all(os.environ.get(k) or values.get(k) for k in keys):
        api = connect()
        user = api.request('GET', '/rest/api/user/current')
        report(True, f'{api.base} accepts the token of "{user.get("displayName") or user.get("publicName")}"')
    print('all set' if not problems else f'{problems} item(s) missing; pull/push of pages without '
          'diagrams only need python, markdown and the credentials')
    sys.exit(0 if not problems else EXIT_DEPENDENCY)


def cmd_check(args):
    api = connect()
    user = api.request('GET', '/rest/api/user/current')
    print(f'ok: {api.base} accepts the token of "{user.get("displayName") or user.get("publicName")}"')


def cmd_info(args):
    api = connect()
    page = api.page(page_id_from_url(args.url, api.base))
    space = api.space(page['spaceId'])
    parent = api.page(page['parentId']) if page.get('parentId') else None
    version = page['version']
    print(f'title:    {page["title"]}\nid:       {page["id"]}\n'
          f'space:    {space.get("name")} ({space.get("key")})\n'
          f'parent:   {parent["title"] + " (id " + parent["id"] + ")" if parent else "-"}\n'
          f'version:  {version["number"]} ({version.get("createdAt", "")[:19]}'
          f'{", " + repr(version["message"]) if version.get("message") else ""})\n'
          f'url:      {api.web_url(page)}')
    storage = page_storage(page)
    try:
        storage_to_markdown(page['title'], storage)
        print('markdown: pull / push can carry everything on this page')
    except ConversionError as e:
        print('markdown: not everything fits; edit with get-storage / put-storage:')
        for p in e.problems[:10]:
            print(f'    {p}')


def cmd_pull(args):
    need_markdown()
    api = connect()
    page = api.page(page_id_from_url(args.url, api.base))
    try:
        text = storage_to_markdown(page['title'], page_storage(page))
    except ConversionError as e:
        fail('the page holds content Markdown cannot carry; use get-storage / put-storage:\n' +
             '\n'.join(f'    {p}' for p in e.problems))
    out = pathlib.Path(args.output) if args.output else default_output(page, '.md')
    out.write_text(text, encoding='utf-8')
    write_sidecar(out, api, page)
    print(f'pulled "{page["title"]}" (version {page["version"]["number"]}) -> {out}')


def cmd_push(args):
    need_markdown()
    local = pathlib.Path(args.file)
    side = read_sidecar(local)
    api = connect()
    page = guarded_page(api, side, args.force)
    text = local.read_text(encoding='utf-8')
    next_version = page['version']['number'] + 1
    title, storage, _ = to_storage(text, local, diagram_builder(api, page, next_version))
    check_page_rules(storage, str(local))
    try:
        before = storage_to_markdown(page['title'], page_storage(page))
    except ConversionError as e:
        if not args.force:
            fail('the page now holds content Markdown cannot carry, which a push would drop:\n' +
                 '\n'.join(f'    {p}' for p in e.problems) + '\nUse get-storage / put-storage.')
        before = ''
    after = storage_to_markdown(title, storage)
    print(f'{"would update" if args.dry_run else "update"} "{page["title"]}" '
          f'(version {page["version"]["number"]} -> {next_version}):')
    show_diff(before, after, 'page', str(local))
    if args.dry_run:
        return
    if before == after and title == page['title']:
        print('nothing to change')
        return
    page = api.update(page, title, storage, args.message)
    write_sidecar(local, api, api.page(page['id']))
    print(f'updated: {api.web_url(page)}')


def cmd_get_storage(args):
    api = connect()
    page = api.page(page_id_from_url(args.url, api.base))
    out = pathlib.Path(args.output) if args.output else default_output(page, '.xml')
    out.write_text(page_storage(page), encoding='utf-8')
    write_sidecar(out, api, page)
    print(f'saved the source of "{page["title"]}" (version {page["version"]["number"]}) -> {out}')


def cmd_put_storage(args):
    local = pathlib.Path(args.file)
    side = read_sidecar(local)
    api = connect()
    page = guarded_page(api, side, args.force)
    storage = local.read_text(encoding='utf-8')
    check_page_rules(storage, str(local))
    diagrams = []
    for match in ADF_EXTENSION_RE.finditer(storage):
        source = extension_source(parse_storage(match.group(0))[0])
        if source is not None:
            diagrams.append(source)
    check_plantuml(diagrams)
    old = page_storage(page)
    print(f'{"would update" if args.dry_run else "update"} "{page["title"]}" '
          f'(version {page["version"]["number"]} -> {page["version"]["number"] + 1}):')
    show_diff(old.replace('><', '>\n<'), storage.replace('><', '>\n<'), 'page', str(local))
    if args.dry_run or storage == old:
        return
    page = api.update(page, page['title'], storage, args.message)
    write_sidecar(local, api, api.page(page['id']))
    print(f'updated: {api.web_url(page)}')


def cmd_create(args):
    need_markdown()
    api = connect()
    parent = api.page(page_id_from_url(args.parent, api.base))
    local = pathlib.Path(args.file)
    text = local.read_text(encoding='utf-8')
    title, _ = split_title(text, local)
    if api.find(parent['spaceId'], title):
        fail(f'a page titled "{title}" already exists in this space')
    _, storage, sources = to_storage(text, local)  # diagrams need the new page's id: second pass
    check_plantuml(sources)
    check_page_rules(re.sub(r'<!--diagram-\d+-->', '', storage), str(local))
    print(f'{"would create" if args.dry_run else "create"} "{title}" under "{parent["title"]}"')
    if args.dry_run:
        return
    page = api.create(parent['spaceId'], parent['id'], title, re.sub(r'<!--diagram-\d+-->', '', storage))
    if sources:
        page = api.page(page['id'])
        _, storage, _ = to_storage(text, local, diagram_builder(api, page, page['version']['number'] + 1))
        page = api.update(page, title, storage, VERSION_MESSAGE)
    page = api.page(page['id'])
    write_sidecar(local, api, page)
    print(f'created: {api.web_url(page)}')


def cmd_delete(args):
    api = connect()
    page = api.page(page_id_from_url(args.url, api.base))
    if args.confirm_title != page['title']:
        fail(f'--confirm-title must be the exact title "{page["title"]}"', EXIT_USAGE)
    children = api.request('GET', f'/api/v2/pages/{page["id"]}/children').get('results', [])
    if children:
        fail(f'"{page["title"]}" has {len(children)} child page(s); move or delete them first')
    api.request('DELETE', f'/api/v2/pages/{page["id"]}', retries=0)
    print(f'moved "{page["title"]}" to the trash (restore it from the space settings)')


def cmd_plantuml(args):
    if args.action == 'decode':
        print(plantuml_decode(args.value))
    else:
        print(plantuml_encode(pathlib.Path(args.value).read_text(encoding='utf-8').rstrip('\n')))


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                 epilog=__doc__.split('\n\n', 1)[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('doctor', help='check every dependency and the credentials')
    p.add_argument('--offline', action='store_true', help='do not try the token against the site')
    p.set_defaults(run=cmd_doctor)
    sub.add_parser('check').set_defaults(run=cmd_check)
    p = sub.add_parser('info'); p.add_argument('url'); p.set_defaults(run=cmd_info)
    p = sub.add_parser('pull'); p.add_argument('url'); p.add_argument('-o', '--output')
    p.set_defaults(run=cmd_pull)
    for name, run in (('push', cmd_push), ('put-storage', cmd_put_storage)):
        p = sub.add_parser(name); p.add_argument('file')
        p.add_argument('--dry-run', action='store_true', help='show the change, write nothing')
        p.add_argument('--force', action='store_true', help='overwrite changes made since the pull')
        p.add_argument('-m', '--message', default=VERSION_MESSAGE, help='version comment')
        p.set_defaults(run=run)
    p = sub.add_parser('get-storage'); p.add_argument('url'); p.add_argument('-o', '--output')
    p.set_defaults(run=cmd_get_storage)
    p = sub.add_parser('create'); p.add_argument('file'); p.add_argument('--parent', required=True)
    p.add_argument('--dry-run', action='store_true'); p.set_defaults(run=cmd_create)
    p = sub.add_parser('delete'); p.add_argument('url'); p.add_argument('--confirm-title', required=True)
    p.set_defaults(run=cmd_delete)
    p = sub.add_parser('plantuml'); p.add_argument('action', choices=('decode', 'encode'))
    p.add_argument('value', help='decode: the data field; encode: a .puml file')
    p.set_defaults(run=cmd_plantuml)
    args = ap.parse_args()
    try:
        args.run(args)
    except ConnectionError as e:
        fail(f'network: {e}')


if __name__ == '__main__':
    main()
