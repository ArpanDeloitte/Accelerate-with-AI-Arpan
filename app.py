"""
QueryGenie - local DB bridge (multi-connection)
------------------------------------------------------
READ-ONLY metadata service so the browser app can browse live Oracle tables/columns.

- Manages named connections (Basic host/port  or  Autonomous DB wallet).
- Wallet .zip is uploaded from the app, unzipped locally, and stored per connection.
- Credentials live ONLY on this machine (connections.json + connections/), git-ignored.
  Passwords are WRITE-ONLY: never returned to the browser.
- python-oracledb THIN mode (no Instant Client needed).
- Binds to 127.0.0.1 only. Only fixed, parameterized data-dictionary queries.

Run:  pip install -r requirements.txt   then   python app.py
"""

import os
import io, json, uuid, zipfile, shutil, threading, re
import oracledb
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()
BASE      = os.path.dirname(os.path.abspath(__file__))
CONN_DIR  = os.path.join(BASE, "connections")     # per-connection wallets live here
STAGE_DIR = os.path.join(BASE, ".staging")        # temporary uploaded wallets
STORE     = os.path.join(BASE, "connections.json")  # metadata incl. secrets (LOCAL ONLY)
PORT      = int(os.getenv("BRIDGE_PORT", "8788"))

os.makedirs(CONN_DIR, exist_ok=True)
os.makedirs(STAGE_DIR, exist_ok=True)


ERR_INVALID_NAME = "Invalid connection name"

def _safe_conn_dir(name):
    """Resolve CONN_DIR/<connection-name> to a path that cannot escape CONN_DIR.
    secure_filename() strips path separators and traversal (`..`, `/`, `\\`),
    neutralising the HTTP-supplied name before it is used as a filesystem path
    (CWE-23 path traversal). Legit names (letters/digits/_/-) are unchanged."""
    safe = secure_filename(name or "")
    if not safe:
        raise ValueError(ERR_INVALID_NAME)
    return os.path.join(CONN_DIR, safe)

def _safe_extract(zf, dest):
    """Extract an archive member-by-member, writing each entry only if it stays
    inside `dest`. extractall() is never used, so a crafted archive cannot write
    outside the destination directory (CWE-22 zip slip)."""
    dest_abs = os.path.abspath(dest)
    for member in zf.infolist():
        target = os.path.abspath(os.path.join(dest_abs, member.filename))
        if target != dest_abs and not target.startswith(dest_abs + os.sep):
            raise ValueError("Unsafe path in wallet archive: " + member.filename)
        if member.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)

# CSRF protection is intentionally not enabled. This is a local-only, read-only
# metadata bridge: it binds to 127.0.0.1 (never network-exposed), uses NO session
# or cookie authentication (so there are no browser-attached credentials for a
# forged request to ride on), and CORS is restricted to localhost origins only.
# Traditional browser form-based CSRF therefore does not apply. Re-evaluate if this
# is ever exposed beyond localhost or given cookie/session auth. (S4502)
app = Flask(__name__)  # NOSONAR
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB wallet cap
# Local-only CORS: the app runs from http://localhost:<port> or file:// ("null").
# External web origins are rejected; the bridge itself binds 127.0.0.1 only.
LOCAL_ORIGINS = [re.compile(r"^https?://localhost(:\d+)?$"),
                 re.compile(r"^https?://127\.0\.0\.1(:\d+)?$"), "null"]
CORS(app, resources={r"/*": {"origins": LOCAL_ORIGINS}})

# --- Demo data (used when a connection is marked kind='demo'; no real DB) ---------
DEMO_TABLES = [
    {"owner": "DEMO", "table": "AP_INVOICES_ALL",      "numRows": 1200},
    {"owner": "DEMO", "table": "AP_INVOICE_LINES_ALL", "numRows": 5400},
    {"owner": "DEMO", "table": "GL_JE_HEADERS",        "numRows": 800},
]
NUM15 = "NUMBER(15)"          # most common demo column type
TNSNAMES = "tnsnames.ora"      # wallet marker file

DEMO_COLUMNS = {
    "AP_INVOICES_ALL": [
        {"name": "INVOICE_ID",     "dataType": NUM15,   "nullable": False, "pk": True,  "comment": "Primary key"},
        {"name": "VENDOR_ID",      "dataType": NUM15,   "nullable": True,  "pk": False, "comment": "Supplier"},
        {"name": "INVOICE_AMOUNT", "dataType": "NUMBER(14,2)", "nullable": True,  "pk": False, "comment": None},
        {"name": "INVOICE_DATE",   "dataType": "DATE",         "nullable": True,  "pk": False, "comment": None},
    ],
    "AP_INVOICE_LINES_ALL": [
        {"name": "INVOICE_ID",      "dataType": NUM15,   "nullable": False, "pk": True, "comment": "FK -> AP_INVOICES_ALL"},
        {"name": "LINE_NUMBER",     "dataType": "NUMBER(10)",   "nullable": False, "pk": True, "comment": None},
        {"name": "AMOUNT",          "dataType": "NUMBER(14,2)", "nullable": True,  "pk": False,"comment": None},
    ],
    "GL_JE_HEADERS": [
        {"name": "JE_HEADER_ID", "dataType": NUM15, "nullable": False, "pk": True, "comment": "Primary key"},
        {"name": "LEDGER_ID",    "dataType": NUM15, "nullable": True,  "pk": False,"comment": None},
        {"name": "STATUS",       "dataType": "VARCHAR2(1)","nullable": True,  "pk": False,"comment": None},
    ],
}

# --- store helpers ----------------------------------------------------------------
def load_store():
    if os.path.exists(STORE):
        try:
            with open(STORE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_store(d):
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)

def public(c):
    """A connection without any secrets - safe to send to the browser."""
    return {
        "name": c["name"], "kind": c["kind"], "env": c.get("env", "DEV"),
        "target": c.get("target", ""), "status": c.get("status", "unknown"),
        "readonly": c.get("readonly", True), "schema": c.get("schema", ""),
        "user": c.get("username", ""),
    }

def _find_wallet_dir(root):
    if os.path.exists(os.path.join(root, TNSNAMES)):
        return root
    for dp, _, files in os.walk(root):
        if TNSNAMES in files:
            return dp
    return root

def parse_tns_aliases(wallet_dir):
    p = os.path.join(wallet_dir, TNSNAMES)
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8", errors="ignore") as f:
        txt = f.read()
    out, seen = [], set()
    for m in re.finditer(r'^\s*([A-Za-z0-9_$#]+)\s*=', txt, re.M):
        a = m.group(1)
        if a.upper() in seen or a.lower() in ("description", "address", "connect_data", "security"):
            continue
        seen.add(a.upper()); out.append(a)
    return out

# Seconds to wait for the TCP/connect handshake before giving up. Prevents the
# "stuck for minutes" hang when the DB host is unreachable (firewall / no VPN).
CONNECT_TIMEOUT = int(os.getenv("CONNECT_TIMEOUT", "20"))

# Enable THICK mode if the bundled Oracle Instant Client is present. Thick mode uses the
# wallet's cwallet.sso (auto-login) with NO wallet password - exactly like SQL Developer.
def _find_instant_client():
    base = os.path.join(BASE, "instantclient")
    if not os.path.isdir(base):
        return None
    for dp, _, files in os.walk(base):
        if any(f.lower() == "oci.dll" for f in files) or any(f.startswith("libclntsh") for f in files):
            return dp
    return None

THICK = False
_ic = _find_instant_client()
if _ic:
    try:
        oracledb.init_oracle_client(lib_dir=_ic)
        THICK = True
        print("Oracle Instant Client loaded (THICK mode) from", _ic)
    except Exception as _e:
        print("Instant Client present but init failed (%s); using THIN mode" % str(_e)[:100])
else:
    print("No Instant Client bundled; using THIN mode (encrypted wallets need a wallet password)")

def _extract_balanced(text, open_idx):
    """From `open_idx` (index of an opening '('), return the substring up to its
    matching ')', counting nested parens. Handles TNS entries that wrap across
    multiple lines (a plain '$'-anchored regex only captures the first line)."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return text[open_idx:i + 1]
    return text[open_idx:]  # unbalanced (shouldn't happen) - best effort

def _thick_dsn(wdir, alias):
    """Full TNS descriptor for `alias` with the wallet dir embedded, so thick-mode connects
    need no alias lookup (avoids ORA-12154 from a stale/cross-connection TNS cache).
    Falls back to the bare alias if the descriptor can't be parsed."""
    p = os.path.join(wdir, TNSNAMES)
    if not (alias and os.path.isfile(p)):
        return alias
    try:
        txt = open(p, encoding="utf-8", errors="ignore").read()
    except Exception:
        return alias
    m = re.search(r'(?im)^\s*' + re.escape(alias) + r'\s*=\s*\(', txt)
    if not m:
        return alias
    desc = _extract_balanced(txt, m.end() - 1).strip()
    if "my_wallet_directory" not in desc.lower():
        wp = wdir.replace("\\", "\\\\")
        if re.search(r'(?i)\(security\s*=', desc):
            desc = re.sub(r'(?i)(\(security\s*=)', r'\1(MY_WALLET_DIRECTORY="' + wp + '")', desc, count=1)
        else:
            i = desc.rfind(")")
            desc = desc[:i] + '(SECURITY=(MY_WALLET_DIRECTORY="' + wp + '"))' + desc[i:]
    return desc

def _wallet_missing_files(wdir):
    """Which of the files actually needed to connect are missing from this wallet
    folder. An incomplete/corrupted wallet .zip (partial extraction, a renamed
    duplicate download, etc.) is a common real-world cause of connect failures
    that otherwise surface as a cryptic Oracle error (e.g. ORA-06413)."""
    missing = [f for f in ("tnsnames.ora", "sqlnet.ora")
               if not os.path.isfile(os.path.join(wdir, f))]
    if not (os.path.isfile(os.path.join(wdir, "cwallet.sso")) or
            os.path.isfile(os.path.join(wdir, "ewallet.pem"))):
        missing.append("cwallet.sso or ewallet.pem")
    return missing

_WALLET_HINT = (" If this persists: delete this connection (Delete button) and "
                "re-upload a FRESH wallet .zip downloaded directly from OCI "
                "(Autonomous Database -> DB Connection -> Download Wallet) - not "
                "a renamed/duplicate copy, which is often incomplete.")

# These ORA codes mean the wallet/network layer already worked (Oracle received and
# rejected a logon attempt) - the wallet itself is fine, so _WALLET_HINT would send
# the user chasing the wrong fix. Show a credentials-specific hint instead.
_AUTH_ERROR_CODES = ("ORA-01017", "ORA-01005", "ORA-28000", "ORA-01047", "ORA-28001")
_AUTH_HINT = (" This is a username/password problem, not a wallet problem: double-check "
              "the DB username and password entered above are correct for this Autonomous "
              "Database (case-sensitive), and that the account isn't locked or expired.")

def _connect_error_hint(msg):
    return _AUTH_HINT if any(code in msg for code in _AUTH_ERROR_CODES) else _WALLET_HINT

def connect_with(c):
    kind = c["kind"]
    if kind == "basic":
        dsn = "{}:{}/{}".format(c.get("host"), c.get("port", "1521"), c.get("service"))
        return oracledb.connect(user=c.get("username"), password=c.get("password"), dsn=dsn,
                                tcp_connect_timeout=CONNECT_TIMEOUT, retry_count=0)
    if kind == "wallet":
        wdir = os.path.join(_safe_conn_dir(c["name"]), "wallet")
        missing = _wallet_missing_files(wdir)
        if missing:
            raise ValueError("Wallet folder is missing " + ", ".join(missing) +
                             " - it looks incomplete or corrupted." + _WALLET_HINT)
        if THICK:
            # Auto-login SSO wallet (cwallet.sso) - no wallet password (like SQL Developer).
            # Full descriptor with wallet dir embedded -> no alias lookup, cache-proof.
            os.environ["TNS_ADMIN"] = wdir
            try:
                return oracledb.connect(user=c.get("username"), password=c.get("password"),
                                        dsn=_thick_dsn(wdir, c.get("service")))
            except Exception as e:
                raise type(e)(str(e) + _connect_error_hint(str(e))) from e
        # --- THIN mode fallback: needs the encrypted ewallet.pem's password ---
        pem = os.path.join(wdir, "ewallet.pem")
        if not c.get("walletPassword") and os.path.isfile(pem):
            try:
                head = open(pem, "r", encoding="utf-8", errors="ignore").read(256)
            except Exception:
                head = ""
            if "ENCRYPTED" in head.upper():
                raise ValueError("Wallet password required (thin mode): this wallet's ewallet.pem is "
                                 "encrypted. Either enter the wallet password, or keep the bundled "
                                 "Instant Client so the bridge can use the SSO wallet like SQL Developer.")
        kwargs = {"user": c.get("username"), "password": c.get("password"), "dsn": c.get("service"),
                      "config_dir": wdir, "wallet_location": wdir,
                      "tcp_connect_timeout": CONNECT_TIMEOUT, "retry_count": 0}
        if c.get("walletPassword"):
            kwargs["wallet_password"] = c["walletPassword"]
        try:
            return oracledb.connect(**kwargs)
        except Exception as e:
            raise type(e)(str(e) + _connect_error_hint(str(e))) from e
    raise ValueError("Demo connection has no live database")

# tcp_connect_timeout only bounds the TCP phase. The TLS/login phase can still stall
# (corporate TLS-inspecting proxy breaking wallet mTLS, a stopped Autonomous DB, or an
# ACL dropping packets). So run the whole connect in a worker thread with a hard cap.
OVERALL_TIMEOUT = int(os.getenv("OVERALL_TIMEOUT", str(CONNECT_TIMEOUT + 10)))

def connect_with_timeout(c):
    result = {}
    def run():
        try:
            result["conn"] = connect_with(c)
        except Exception as e:
            result["err"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(OVERALL_TIMEOUT)
    if th.is_alive():
        raise TimeoutError(
            "Connect timed out after %ds. TCP reaches the DB but TLS/login never completes. "
            "Usual causes: a corporate TLS-inspecting proxy/VPN breaking the wallet's mutual-TLS, "
            "the Autonomous DB being stopped, or a network ACL dropping traffic. "
            "Verify the DB is RUNNING and try a network without TLS inspection." % OVERALL_TIMEOUT
        )
    if "err" in result:
        raise result["err"]
    return result["conn"]

def _truthy(v):
    return str(v).lower() in ("true", "1", "on", "yes")

# --- endpoints --------------------------------------------------------------------
@app.get("/health")
def health():
    store = load_store()
    return jsonify(status="ok", connectionCount=len(store),
                   driver="python-oracledb (%s)" % ("thick" if THICK else "thin"))

@app.post("/wallet/services")
def wallet_services():
    """Upload a wallet .zip; unzip to a staging area; return TNS aliases + a token."""
    f = request.files.get("wallet")
    if not f:
        return jsonify(status="error", error="No wallet file uploaded"), 400
    token = uuid.uuid4().hex
    dest = os.path.join(STAGE_DIR, token)
    os.makedirs(dest, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(f.read())) as z:
            _safe_extract(z, dest)
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)
        return jsonify(status="error", error="Not a valid .zip: " + str(e)), 400
    wdir = _find_wallet_dir(dest)
    return jsonify(status="ok", token=token, services=parse_tns_aliases(wdir))

@app.get("/connections")
def list_connections():
    store = load_store()
    return jsonify(status="ok", connections=[public(c) for c in store.values()])

def _persist_wallet(name, d):
    """Move a staged wallet into CONN_DIR/name/wallet (flat). Returns error str or None."""
    token = d.get("walletToken")
    staged = os.path.join(STAGE_DIR, token) if token else None
    try:
        conn_root = _safe_conn_dir(name)
    except ValueError:
        return ERR_INVALID_NAME
    wdst = os.path.join(conn_root, "wallet")
    if staged and os.path.isdir(staged):
        wsrc = _find_wallet_dir(staged)
        shutil.rmtree(conn_root, ignore_errors=True)
        os.makedirs(wdst, exist_ok=True)
        for fn in os.listdir(wsrc):
            sp = os.path.join(wsrc, fn)
            if os.path.isfile(sp):
                shutil.copy2(sp, os.path.join(wdst, fn))
        shutil.rmtree(staged, ignore_errors=True)
    if not os.path.isfile(os.path.join(wdst, TNSNAMES)):
        return ("Wallet not found for this connection. Click 'Upload Wallet .zip' "
                "(it must contain tnsnames.ora) and try again.")
    return None

def _verify_and_mark(c):
    """Connect-test a non-demo connection; set c['status'] / c['error'] in place."""
    if c["kind"] == "demo":
        c["status"] = "ok"
        return
    try:
        with connect_with_timeout(c) as conn:
            cur = conn.cursor(); cur.execute("select 1 from dual"); cur.fetchone()
        c["status"] = "ok"; c.pop("error", None)
    except Exception as e:
        c["status"] = "error"; c["error"] = str(e)

def _conn_record(d, name):
    """Build the connection dict from request fields (defaults applied)."""
    demo = _truthy(d.get("demo"))
    kind = "demo" if demo else (d.get("kind") or "wallet")
    return {
        "name": name, "kind": kind, "env": (d.get("env") or "DEV"),
        "username": (d.get("username") or ""), "password": (d.get("password") or ""),
        "host": (d.get("host") or ""), "port": (d.get("port") or "1521"),
        "service": (d.get("service") or ""), "walletPassword": (d.get("walletPassword") or ""),
        "schema": (d.get("schema") or "").upper(),
        "readonly": _truthy(d.get("readonly", "true")),
    }

def _apply_target(c, name, d):
    """Validate + set c['target'] per kind. Returns an error string or None."""
    kind = c["kind"]
    if kind == "wallet":
        err = _persist_wallet(name, d)
        if err:
            return err
        c["target"] = c["service"] or "(wallet)"
    elif kind == "basic":
        if not (c["host"] and c["service"]):
            return "Host and Service are required"
        c["target"] = "{}:{}/{}".format(c["host"], c["port"], c["service"])
    else:  # demo
        c["target"] = "mock (demo)"
    return None

@app.post("/connections")
def save_connection():
    d = request.form if request.form else (request.json or {})
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(status="error", error="Connection name is required"), 400
    c = _conn_record(d, name)
    store = load_store()
    err = _apply_target(c, name, d)
    if err:
        return jsonify(status="error", error=err), 400
    _verify_and_mark(c)
    store[name] = c
    save_store(store)
    return jsonify(status="ok", connection=public(c), error=c.get("error"))

@app.route("/connections/<name>", methods=["DELETE"])
def delete_connection(name):
    try:
        target = _safe_conn_dir(name)
    except ValueError:
        return jsonify(status="error", error=ERR_INVALID_NAME), 400
    store = load_store()
    if name in store:
        store.pop(name); save_store(store)
    shutil.rmtree(target, ignore_errors=True)
    return jsonify(status="ok")

@app.get("/connections/<name>/test")
def test_connection(name):
    store = load_store(); c = store.get(name)
    if not c:
        return jsonify(status="error", error="Unknown connection"), 404
    if c["kind"] == "demo":
        c["status"] = "ok"; save_store(store)
        return jsonify(status="ok", connected=True, demo=True)
    try:
        with connect_with_timeout(c) as conn:
            cur = conn.cursor()
            cur.execute("select user, sys_context('userenv','db_name') from dual")
            user, db_name = cur.fetchone()
        c["status"] = "ok"; c.pop("error", None); save_store(store)
        return jsonify(status="ok", connected=True, user=user, dbName=db_name)
    except Exception as e:
        c["status"] = "error"; c["error"] = str(e); save_store(store)
        return jsonify(status="error", connected=False, error=str(e)), 500

@app.get("/tables")
def tables():
    store = load_store(); c = store.get(request.args.get("conn"))
    if not c:
        return jsonify(status="error", error="Unknown connection (pass ?conn=name)"), 404
    owner = (request.args.get("owner") or c.get("schema") or "").strip().upper()
    q = (request.args.get("q") or "").strip().upper()
    if c["kind"] == "demo":
        rows = [t for t in DEMO_TABLES if not q or q in t["table"]]
        return jsonify(status="ok", tables=rows, count=len(rows))
    binds, where = {}, []
    if owner:
        # A specific schema was named -> list that schema's tables.
        base = "select owner, table_name, num_rows, last_analyzed from all_tables"
        where.append("owner = :owner"); binds["owner"] = owner
    elif q:
        # Autosuggest: search ALL tables the user can see, across schemas, by name.
        base = "select owner, table_name, num_rows, last_analyzed from all_tables"
    else:
        # No filter -> just the connected user's own tables (avoids dumping the whole catalog).
        base = "select user as owner, table_name, num_rows, last_analyzed from user_tables"
    if q:
        where.append("upper(table_name) like :q"); binds["q"] = "%" + q + "%"
    sql = base + ((" where " + " and ".join(where)) if where else "") + \
          " order by table_name fetch first 500 rows only"
    try:
        with connect_with_timeout(c) as conn:
            cur = conn.cursor(); cur.execute(sql, binds)
            rows = [{"owner": r[0], "table": r[1], "numRows": r[2],
                     "lastAnalyzed": r[3].isoformat() if r[3] else None} for r in cur]
        return jsonify(status="ok", tables=rows, count=len(rows))
    except Exception as e:
        return jsonify(status="error", error=str(e)), 500

def _col_queries(owner):
    """(col_sql, pk_sql, cm_sql) for ALL_* (owner given) or USER_* (no owner)."""
    if owner:
        return (
            "select column_name,data_type,data_length,data_precision,data_scale,nullable,column_id "
            "from all_tab_columns where owner=:o and table_name=:t order by column_id",
            "select cc.column_name from all_constraints c join all_cons_columns cc "
            "on c.owner=cc.owner and c.constraint_name=cc.constraint_name "
            "where c.constraint_type='P' and c.owner=:o and c.table_name=:t",
            "select column_name,comments from all_col_comments where owner=:o and table_name=:t",
        )
    return (
        "select column_name,data_type,data_length,data_precision,data_scale,nullable,column_id "
        "from user_tab_columns where table_name=:t order by column_id",
        "select cc.column_name from user_constraints c join user_cons_columns cc "
        "on c.constraint_name=cc.constraint_name "
        "where c.constraint_type='P' and c.table_name=:t",
        "select column_name,comments from user_col_comments where table_name=:t",
    )

def _fmt_type(dtype, length, prec, scale):
    if dtype in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "RAW"):
        return "{}({})".format(dtype, length)
    if dtype == "NUMBER" and prec:
        return "NUMBER({}{})".format(prec, ("," + str(scale)) if scale else "")
    return dtype

def _load_columns(conn, owner, table):
    col_sql, pk_sql, cm_sql = _col_queries(owner)
    binds = {"o": owner, "t": table} if owner else {"t": table}
    cur = conn.cursor()
    cur.execute(pk_sql, binds); pks = {r[0] for r in cur}
    cur.execute(cm_sql, binds); comments = {r[0]: r[1] for r in cur}
    cur.execute(col_sql, binds)
    cols = []
    for name_, dtype, length, prec, scale, nullable, pos in cur:
        cols.append({"name": name_, "dataType": _fmt_type(dtype, length, prec, scale),
                     "rawType": dtype, "nullable": nullable == "Y", "position": pos,
                     "pk": name_ in pks, "comment": comments.get(name_)})
    return cols

@app.get("/columns")
def columns():
    store = load_store(); c = store.get(request.args.get("conn"))
    if not c:
        return jsonify(status="error", error="Unknown connection (pass ?conn=name)"), 404
    table = (request.args.get("table") or "").strip().upper()
    owner = (request.args.get("owner") or c.get("schema") or "").strip().upper()
    if not table:
        return jsonify(status="error", error="'table' is required"), 400
    if c["kind"] == "demo":
        cols = DEMO_COLUMNS.get(table, [])
        return jsonify(status="ok", table=table, owner="DEMO", columns=cols, count=len(cols))
    try:
        with connect_with_timeout(c) as conn:
            cols = _load_columns(conn, owner, table)
        if not cols:
            return jsonify(status="error", error="No columns (check name/owner/grants)"), 404
        return jsonify(status="ok", table=table, owner=(owner or None), columns=cols, count=len(cols))
    except Exception as e:
        return jsonify(status="error", error=str(e)), 500


if __name__ == "__main__":
    print("==> QueryGenie DB bridge  ->  http://localhost:%d" % PORT)
    print("    Manage connections in the app's 'Database Connections' screen.")
    app.run(host="127.0.0.1", port=PORT, debug=False)
