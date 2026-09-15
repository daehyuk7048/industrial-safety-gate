import os, uuid, json, sqlite3
from datetime import datetime, date
from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-production')

# =======================
# DB 경로 고정 (절대경로)
# =======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "database.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# =======================
# 테이블 보장
# =======================
def ensure_tables():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
      employeeId   TEXT PRIMARY KEY,
      employeeName TEXT,
      birthDate    TEXT
    )
    """)

    # logs: checkInTime 스키마(최신)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS logs(
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      employeeName TEXT,
      date         TEXT,
      checkInTime  TEXT,
      safetyCheck  TEXT,
      source       TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS schedules(
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      scheduleDate TEXT,
      employeeId   TEXT,
      teamName     TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS notices(
      id        INTEGER PRIMARY KEY AUTOINCREMENT,
      title     TEXT,
      content   TEXT,
      createdAt TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admins(
      id       INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT,
      password TEXT
    )
    """)

    # RFID 매핑 테이블
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rfid_tags(
      rfidUid    TEXT PRIMARY KEY,
      employeeId TEXT NOT NULL
    )
    """)

    # QR 티켓
    cur.execute("""
    CREATE TABLE IF NOT EXISTS qr_tickets(
      ticketId   TEXT PRIMARY KEY,
      employeeId TEXT,
      gateId     TEXT,
      status     TEXT,        -- pending | verifying | done
      createdAt  TEXT,
      updatedAt  TEXT,
      resultJson TEXT
    )
    """)

    conn.commit()
    conn.close()

# logs 스키마 마이그레이션(구 workHours → checkInTime)
def migrate_schema():
    conn = get_db_connection()
    cur  = conn.cursor()
    cols = [r["name"] for r in cur.execute("PRAGMA table_info(logs)").fetchall()]
    if "checkInTime" not in cols:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS logs_new(
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          employeeName TEXT,
          date         TEXT,
          checkInTime  TEXT,
          safetyCheck  TEXT,
          source       TEXT
        )
        """)
        if "workHours" in cols:
            cur.execute("""
                INSERT INTO logs_new (employeeName, date, checkInTime, safetyCheck, source)
                SELECT employeeName, date, '00:00:00', safetyCheck, source FROM logs
            """)
        cur.execute("DROP TABLE logs")
        cur.execute("ALTER TABLE logs_new RENAME TO logs")
        conn.commit()
    conn.close()

# 과거 로그 정규화: logs.employeeName에 사번이 들어간 경우 이름으로 치환
def normalize_log_names():
    conn = get_db_connection()
    cur  = conn.cursor()
    # users.employeeId = logs.employeeName 인 경우 이름으로 업데이트
    cur.execute("""
        UPDATE logs
        SET employeeName = (
            SELECT u.employeeName FROM users u WHERE u.employeeId = logs.employeeName
        )
        WHERE EXISTS (SELECT 1 FROM users u WHERE u.employeeId = logs.employeeName)
    """)
    conn.commit()
    conn.close()

# 초기 RFID 매핑(임시 시드)
def seed_rfid_tags(mapping: dict):
    conn = get_db_connection()
    cur  = conn.cursor()
    for uid, emp_id in mapping.items():
        cur.execute("""
        INSERT INTO rfid_tags(rfidUid, employeeId)
        VALUES(?,?)
        ON CONFLICT(rfidUid) DO UPDATE SET employeeId=excluded.employeeId
        """, (uid, emp_id))
    conn.commit()
    conn.close()

# =======================
# 공용 유틸
# =======================
def now_date():
    return datetime.now().strftime('%Y-%m-%d')

def now_ts():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def now_hms():
    return datetime.now().strftime('%H:%M:%S')

# =======================
# API 엔드포인트들
# =======================

# 로그 조회(사원별 필터) — 항상 '이름'으로 반환되도록 JOIN
@app.route('/api/logs', methods=['GET'])
def get_logs():
    employee_name = request.args.get('employeeName')
    conn = get_db_connection()
    cur  = conn.cursor()

    # COALESCE(u.employeeName, logs.employeeName)로 표시용 이름 고정
    base_sql = """
        SELECT
          l.id, l.date, COALESCE(u.employeeName, l.employeeName) AS employeeName,
          l.checkInTime, l.safetyCheck, l.source
        FROM logs l
        LEFT JOIN users u
          ON u.employeeId = l.employeeName     -- l.employeeName이 사번일 수도 있어서 매핑
    """
    params = []
    if employee_name:
        # 필터는 '이름 또는 사번' 모두 허용
        base_sql += " WHERE (l.employeeName = ? OR u.employeeName = ?) "
        params += [employee_name, employee_name]
    base_sql += " ORDER BY l.id DESC"

    rows = cur.execute(base_sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# 수동 로그 추가(폼의 checkInTime을 존중)
@app.route('/api/add_log', methods=['POST'])
def add_log():
    new_log = request.json or {}
    emp_name = new_log.get('employeeName')
    check_in = new_log.get('checkInTime') or now_hms()   # 폼 값 사용, 없으면 현재시간
    safety   = new_log.get('safetyCheck', '양호')
    conn = get_db_connection()
    conn.execute(
        'INSERT INTO logs (employeeName, date, checkInTime, safetyCheck, source) VALUES (?,?,?,?,?)',
        (emp_name, now_date(), check_in, safety, 'manual')
    )
    conn.commit(); conn.close()
    return jsonify({'status': 'success', 'message': 'Log added successfully'})

# 직원 로그인 (employeeName + birthDate)
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    name = data.get('employeeName')
    birth_date = data.get('birthDate')
    conn = get_db_connection()
    user = conn.execute(
        'SELECT * FROM users WHERE employeeName = ? AND birthDate = ?',
        (name, birth_date)
    ).fetchone()
    conn.close()
    if user:
        session['employeeId'] = user['employeeId']
        session['employeeName'] = user['employeeName']
        return jsonify({'status': 'success', 'message': 'Login successful'})
    return jsonify({'status': 'error', 'message': '이름 또는 생년월일이 일치하지 않습니다.'}), 401

# 관리자 로그인
@app.route('/api/admin_login', methods=['POST'])
def admin_login():
    data = request.json or {}
    username = data.get('username')
    password = data.get('password')
    conn = get_db_connection()
    admin = conn.execute(
        'SELECT * FROM admins WHERE username = ? AND password = ?',
        (username, password)
    ).fetchone()
    conn.close()
    if admin:
        session['admin_logged_in'] = True
        return jsonify({'status': 'success', 'message': 'Admin login successful'})
    return jsonify({'status': 'error', 'message': '아이디 또는 비밀번호가 일치하지 않습니다.'}), 401

# 내 로그
@app.route('/api/my_logs')
def get_my_logs():
    if 'employeeName' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    conn = get_db_connection()
    rows = conn.execute(
        # 내 이름으로 저장된 기록 + 혹시 사번으로 저장된 기록까지 매핑
        """
        SELECT l.id, l.date, COALESCE(u.employeeName, l.employeeName) AS employeeName,
               l.checkInTime, l.safetyCheck, l.source
        FROM logs l
        LEFT JOIN users u ON u.employeeId = l.employeeName
        WHERE (l.employeeName = ? OR u.employeeName = ?)
        ORDER BY l.id DESC
        """,
        (session['employeeName'], session['employeeName'])
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# 오늘의 팀원 (employeeId 기준)
@app.route('/api/my_team')
def get_my_team():
    if 'employeeId' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    today_str = now_date()
    conn = get_db_connection()
    my_team_info = conn.execute(
        'SELECT teamName FROM schedules WHERE scheduleDate = ? AND employeeId = ?',
        (today_str, session['employeeId'])
    ).fetchone()
    if not my_team_info:
        conn.close()
        return jsonify({'teamName': '배정된 팀 없음', 'teammates': []})
    team_name = my_team_info['teamName']
    mates = conn.execute(
        'SELECT u.employeeName FROM schedules s JOIN users u ON s.employeeId = u.employeeId '
        'WHERE s.scheduleDate = ? AND s.teamName = ?',
        (today_str, team_name)
    ).fetchall()
    conn.close()
    return jsonify({'teamName': team_name, 'teammates': [m['employeeName'] for m in mates]})

# 내 스케줄
@app.route('/api/my_schedule')
def get_my_schedule():
    if 'employeeId' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    conn = get_db_connection()
    rows = conn.execute(
        'SELECT scheduleDate, teamName FROM schedules WHERE employeeId = ?',
        (session['employeeId'],)
    ).fetchall()
    conn.close()
    return jsonify({r['scheduleDate']: r['teamName'] for r in rows})

# 공지 조회/추가
@app.route('/api/notices', methods=['GET'])
def get_notices():
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM notices ORDER BY id DESC').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/add_notice', methods=['POST'])
def add_notice():
    data = request.json or {}
    conn = get_db_connection()
    conn.execute(
        'INSERT INTO notices (title, content, createdAt) VALUES (?,?,?)',
        (data.get('title'), data.get('content'), now_date())
    )
    conn.commit(); conn.close()
    return jsonify({'status': 'success', 'message': 'Notice added successfully'})

# 수동 체크인(사번)
@app.route('/api/check_in', methods=['POST'])
def check_in():
    data = request.json or {}
    employee_id = data.get('employeeId')
    safety_check = data.get('safetyCheck', '양호')
    if not employee_id:
        return jsonify({'status': 'error', 'message': 'Employee ID is missing'}), 400
    conn = get_db_connection()
    user = conn.execute('SELECT employeeName FROM users WHERE employeeId = ?', (employee_id,)).fetchone()
    if not user:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Unknown employee ID'}), 404
    conn.execute(
        'INSERT INTO logs (employeeName, date, checkInTime, safetyCheck, source) VALUES (?,?,?,?,?)',
        (user['employeeName'], now_date(), now_hms(), safety_check, 'system')
    )
    conn.commit(); conn.close()
    return jsonify({'status': 'success', 'message': f'{user["employeeName"]}님의 출근이 기록되었습니다.'})

# 직원 목록
@app.route('/api/employees', methods=['GET'])
def get_employees():
    conn = get_db_connection()
    rows = conn.execute('SELECT DISTINCT employeeName FROM users ORDER BY employeeName').fetchall()
    conn.close()
    return jsonify([r['employeeName'] for r in rows])

# 현재 로그인 사용자
@app.route('/api/current_user')
def current_user():
    if 'employeeName' in session and 'employeeId' in session:
        return jsonify({'employeeName': session['employeeName'], 'employeeId': session['employeeId']})
    return jsonify({'error': 'Not logged in'}), 401

# 오늘 통계/전체 팀
@app.route('/api/today_stats')
def get_today_stats():
    today_str = now_date()
    conn = get_db_connection()
    total_scheduled = conn.execute(
        'SELECT COUNT(DISTINCT employeeId) FROM schedules WHERE scheduleDate = ?',
        (today_str,)
    ).fetchone()[0]
    safety_ok = conn.execute(
        "SELECT COUNT(*) FROM logs WHERE date = ? AND safetyCheck = '양호'",
        (today_str,)
    ).fetchone()[0]
    safety_nok = conn.execute(
        "SELECT COUNT(*) FROM logs WHERE date = ? AND safetyCheck = '불량'",
        (today_str,)
    ).fetchone()[0]
    conn.close()
    return jsonify({'totalScheduled': total_scheduled, 'safetyOk': safety_ok, 'safetyNok': safety_nok})

@app.route('/api/all_teams_today')
def get_all_teams_today():
    today_str = now_date()
    conn = get_db_connection()
    schedules_rows = conn.execute(
        'SELECT teamName, employeeId FROM schedules WHERE scheduleDate = ? ORDER BY teamName',
        (today_str,)
    ).fetchall()
    teams = {}
    for row in schedules_rows:
        team = row['teamName']
        eid  = row['employeeId']
        if team not in teams: teams[team] = []
        u = conn.execute('SELECT employeeName FROM users WHERE employeeId = ?', (eid,)).fetchone()
        if u: teams[team].append(u['employeeName'])
    conn.close()
    return jsonify(teams)

# =======================
# QR 자동검증 플로우
# =======================

@app.route('/api/qr/start', methods=['POST'])
def qr_start():
    data = request.get_json(force=True)
    employee_id = data.get('employeeId')
    gate_id     = data.get('gateId', 'G1')
    if not employee_id:
        return jsonify({'status':'error','message':'employeeId required'}), 400
    tid = uuid.uuid4().hex[:12].upper()
    conn = get_db_connection()
    conn.execute("""INSERT INTO qr_tickets(ticketId, employeeId, gateId, status, createdAt, updatedAt)
                    VALUES(?,?,?,?,?,?)""",
                 (tid, employee_id, gate_id, 'pending', now_ts(), now_ts()))
    conn.commit(); conn.close()
    return jsonify({'status':'ok','ticketId':tid,'gateId':gate_id})

@app.route('/api/qr/next_task', methods=['GET'])
def qr_next_task():
    gate_id = request.args.get('gate', 'G1')
    conn = get_db_connection()
    row = conn.execute("""
        SELECT ticketId, employeeId, gateId, status, createdAt
        FROM qr_tickets
        WHERE gateId = ? AND status = 'pending'
        ORDER BY createdAt ASC
        LIMIT 1
    """, (gate_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({}), 200

    conn.execute("UPDATE qr_tickets SET status='verifying', updatedAt=? WHERE ticketId=?",
                 (now_ts(), row['ticketId']))
    conn.commit(); conn.close()
    return jsonify({'ticketId': row['ticketId'], 'employeeId': row['employeeId'], 'gateId': row['gateId']})

@app.route('/api/qr/complete', methods=['POST'])
def qr_complete():
    data = request.get_json(force=True)
    tid    = data.get('ticketId')
    ok     = bool(data.get('ok', False))
    helmet = bool(data.get('helmet', False))
    metal  = bool(data.get('metal', False))
    gate_id = data.get('gateId')
    source  = data.get('source', 'QR')
    if not tid:
        return jsonify({'status':'error','message':'ticketId required'}), 400

    conn = get_db_connection()
    t = conn.execute("SELECT employeeId FROM qr_tickets WHERE ticketId=?", (tid,)).fetchone()
    if not t:
        conn.close()
        return jsonify({'status':'error','message':'unknown ticket'}), 404

    employee_id = t['employeeId']
    u = conn.execute("SELECT employeeName FROM users WHERE employeeId=?", (employee_id,)).fetchone()
    employee_name = u['employeeName'] if u else employee_id
    conn.execute(
        "INSERT INTO logs (employeeName, date, checkInTime, safetyCheck, source) VALUES (?,?,?,?,?)",
        (employee_name, now_date(), now_hms(), '양호' if ok else '불량', source)
    )

    result_json = json.dumps({'ok': ok, 'helmet': helmet, 'metal': metal, 'gateId': gate_id}, ensure_ascii=False)
    conn.execute("""UPDATE qr_tickets
                    SET status='done', updatedAt=?, resultJson=?
                    WHERE ticketId=?""", (now_ts(), result_json, tid))
    conn.commit(); conn.close()
    return jsonify({'status':'ok'})

# =======================
# RFID 연동
# =======================

@app.route('/api/rfid/resolve', methods=['GET','POST'])
def rfid_resolve():
    data = request.get_json(silent=True) or {}
    uid = data.get('uid') or request.args.get('uid')
    if not uid:
        return jsonify({'status':'error','message':'uid required'}), 400

    conn = get_db_connection()
    row = conn.execute("SELECT employeeId FROM rfid_tags WHERE rfidUid = ?", (uid,)).fetchone()
    emp = None
    if row:
        emp = conn.execute("SELECT employeeId, employeeName FROM users WHERE employeeId = ?", (row['employeeId'],)).fetchone()
    conn.close()

    if not row or not emp:
        return jsonify({}), 200
    return jsonify({'employeeId': emp['employeeId'], 'employeeName': emp['employeeName']})

@app.route('/api/rfid/start', methods=['POST'])
def rfid_start():
    data = request.get_json(force=True)
    rfid_uid = data.get('rfidUid')
    gate_id  = data.get('gateId', 'G1')
    if not rfid_uid:
        return jsonify({'status':'error','message':'rfidUid required'}), 400

    conn = get_db_connection()
    row = conn.execute("SELECT employeeId FROM rfid_tags WHERE rfidUid = ?", (rfid_uid,)).fetchone()
    emp = None
    if row:
        emp = conn.execute("SELECT employeeId, employeeName FROM users WHERE employeeId = ?", (row['employeeId'],)).fetchone()

    emp_name = (emp['employeeName'] if emp else rfid_uid)
    conn.execute(
        "INSERT INTO logs (employeeName, date, checkInTime, safetyCheck, source) VALUES (?,?,?,?,?)",
        (emp_name, now_date(), now_hms(), '검증중', f'RFID-START[{gate_id}]')
    )
    conn.commit(); conn.close()

    out = {'status':'ok','gateId':gate_id,'rfidUid':rfid_uid}
    if emp: out.update(employeeId=emp['employeeId'], employeeName=emp['employeeName'])
    return jsonify(out)

@app.route('/api/rfid/complete', methods=['POST'])
def rfid_complete():
    data = request.get_json(force=True)
    rfid_uid = data.get('rfidUid')
    ok       = bool(data.get('ok', False))
    helmet   = bool(data.get('helmet', False))
    metal    = bool(data.get('metal', False))
    gate_id  = data.get('gateId', 'G1')

    if not rfid_uid:
        return jsonify({'status':'error','message':'rfidUid required'}), 400

    conn = get_db_connection()
    row = conn.execute("SELECT employeeId FROM rfid_tags WHERE rfidUid = ?", (rfid_uid,)).fetchone()
    emp_name = rfid_uid
    if row:
        u = conn.execute("SELECT employeeName FROM users WHERE employeeId = ?", (row['employeeId'],)).fetchone()
        if u: emp_name = u['employeeName']

    conn.execute(
        "INSERT INTO logs (employeeName, date, checkInTime, safetyCheck, source) VALUES (?,?,?,?,?)",
        (emp_name, now_date(), now_hms(),
         '양호' if ok else '불량', f'RFID-COMPLETE[{gate_id}]')
    )
    conn.commit(); conn.close()
    return jsonify({'status':'ok','result': {'ok': ok, 'helmet': helmet, 'metal': metal}})

@app.route('/api/rfid/bind', methods=['POST'])
def rfid_bind():
    data = request.get_json(force=True)
    rfid_uid   = data.get('rfidUid')
    employeeId = data.get('employeeId')
    if not rfid_uid or not employeeId:
        return jsonify({'status':'error','message':'rfidUid and employeeId required'}), 400
    conn = get_db_connection()
    u = conn.execute("SELECT 1 FROM users WHERE employeeId=?", (employeeId,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'status':'error','message':'unknown employeeId'}), 404
    conn.execute("""
        INSERT INTO rfid_tags(rfidUid, employeeId)
        VALUES(?,?)
        ON CONFLICT(rfidUid) DO UPDATE SET employeeId=excluded.employeeId
    """, (rfid_uid, employeeId))
    conn.commit(); conn.close()
    return jsonify({'status':'ok'})

# =======================
# 페이지 라우팅
# =======================
@app.route('/')
def index():
    if session.get('admin_logged_in'):
        return render_template('admin.html')
    return redirect(url_for('admin_login_page'))

@app.route('/admin_login')
def admin_login_page():
    return render_template('admin_login.html')

@app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

@app.route('/employee')
def employee_main_page():
    if 'employeeName' not in session:
        return redirect(url_for('login_page'))
    return redirect(url_for('qr_generator_page'))

@app.route('/qr_generator')
def qr_generator_page():
    if 'employeeName' in session:
        return render_template('index.html')
    return redirect(url_for('login_page'))

@app.route('/my_work')
def my_work_page():
    if 'employeeName' in session:
        return render_template('my_work.html')
    return redirect(url_for('login_page'))

@app.route('/notices')
def notices_page():
    if 'employeeName' in session:
        return render_template('notices.html')
    return redirect(url_for('login_page'))

@app.route('/scanner')
def scanner_page():
    return render_template('scanner.html')

# =======================
# 서버 실행
# =======================
def _initial_seed_rfid():
    # RFID UID → 사번 초기 매핑(더미). 실제 카드 UID는 /api/rfid/bind 로 등록하거나 여기 값을 바꾸세요.
    mapping = {
        "0000000001": "user001",  # 홍길동
        "0000000002": "user002",  # 김철수
    }
    seed_rfid_tags(mapping)

if __name__ == '__main__':
    ensure_tables()
    migrate_schema()
    _initial_seed_rfid()
    # 과거에 사번으로 저장된 로그를 이름으로 정리(한 번만 실행되어도 무해)
    normalize_log_names()
    app.run(host='0.0.0.0', port=5001, debug=True)
