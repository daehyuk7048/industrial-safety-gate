import os
import sqlite3
from datetime import date, datetime

connection = sqlite3.connect('database.db')
cursor = connection.cursor()

# 모든 테이블을 확실하게 삭제하여 초기화
cursor.execute("DROP TABLE IF EXISTS logs")
cursor.execute("DROP TABLE IF EXISTS users")
cursor.execute("DROP TABLE IF EXISTS teams")
cursor.execute("DROP TABLE IF EXISTS schedules")
cursor.execute("DROP TABLE IF EXISTS notices")
cursor.execute("DROP TABLE IF EXISTS admins")

# --- 테이블 생성 ---
# logs 테이블 (checkInTime 적용)
cursor.execute('''
    CREATE TABLE logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employeeName TEXT NOT NULL,
        date TEXT NOT NULL,
        checkInTime TEXT NOT NULL,
        safetyCheck TEXT NOT NULL,
        source TEXT NOT NULL
    )
''')
# users 테이블
cursor.execute('''
    CREATE TABLE users ( id INTEGER PRIMARY KEY AUTOINCREMENT, employeeId TEXT NOT NULL UNIQUE, 
        employeeName TEXT NOT NULL, birthDate TEXT NOT NULL )
''')
# teams 테이블
cursor.execute('''
    CREATE TABLE teams ( id INTEGER PRIMARY KEY AUTOINCREMENT, teamName TEXT NOT NULL UNIQUE )
''')
# schedules 테이블
cursor.execute('''
    CREATE TABLE schedules ( id INTEGER PRIMARY KEY AUTOINCREMENT, scheduleDate TEXT NOT NULL,
        employeeId TEXT NOT NULL, teamName TEXT NOT NULL )
''')
# notices 테이블
cursor.execute('''
    CREATE TABLE notices ( id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        content TEXT NOT NULL, createdAt TEXT NOT NULL )
''')
# admins 테이블
cursor.execute('''
    CREATE TABLE admins ( id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE, password TEXT NOT NULL )
''')

# --- 테스트 데이터 삽입 ---
today_str = date.today().strftime('%Y-%m-%d')
now_time_str = datetime.now().strftime('%H:%M:%S')

# 사원 데이터 (총 8명) — 공개 저장소용 더미 데이터. 실제 사번/이름/생년월일로 교체해서 사용하세요.
cursor.execute("INSERT INTO users (employeeId, employeeName, birthDate) VALUES (?, ?, ?)", ('user001', '홍길동', '900101'))
cursor.execute("INSERT INTO users (employeeId, employeeName, birthDate) VALUES (?, ?, ?)", ('user002', '김철수', '910202'))
cursor.execute("INSERT INTO users (employeeId, employeeName, birthDate) VALUES (?, ?, ?)", ('user003', '이영희', '920303'))
cursor.execute("INSERT INTO users (employeeId, employeeName, birthDate) VALUES (?, ?, ?)", ('user004', '박민수', '930404'))
cursor.execute("INSERT INTO users (employeeId, employeeName, birthDate) VALUES (?, ?, ?)", ('user005', '최지우', '940505'))
cursor.execute("INSERT INTO users (employeeId, employeeName, birthDate) VALUES (?, ?, ?)", ('user006', '정우진', '950606'))
cursor.execute("INSERT INTO users (employeeId, employeeName, birthDate) VALUES (?, ?, ?)", ('user007', '강서연', '960707'))
cursor.execute("INSERT INTO users (employeeId, employeeName, birthDate) VALUES (?, ?, ?)", ('user008', '윤도현', '970808'))

# 관리자 데이터
# 관리자 비밀번호는 환경변수 ADMIN_PASSWORD 로 지정 (미지정 시 'admin')
cursor.execute("INSERT INTO admins (username, password) VALUES (?, ?)", ('admin', os.environ.get('ADMIN_PASSWORD', 'admin')))

# ▼▼▼ 팀 데이터 삽입 부분의 오타를 수정했습니다 ▼▼▼
cursor.execute("INSERT INTO teams (teamName) VALUES (?)", ('설비1팀',))
cursor.execute("INSERT INTO teams (teamName) VALUES (?)", ('품질1팀',))
cursor.execute("INSERT INTO teams (teamName) VALUES (?)", ('안전2팀',))
# ▲▲▲

# 스케줄 데이터
cursor.execute("INSERT INTO schedules (scheduleDate, employeeId, teamName) VALUES (?, ?, ?)", (today_str, 'user001', '설비1팀'))
cursor.execute("INSERT INTO schedules (scheduleDate, employeeId, teamName) VALUES (?, ?, ?)", (today_str, 'user003', '설비1팀'))
cursor.execute("INSERT INTO schedules (scheduleDate, employeeId, teamName) VALUES (?, ?, ?)", (today_str, 'user004', '설비1팀'))
cursor.execute("INSERT INTO schedules (scheduleDate, employeeId, teamName) VALUES (?, ?, ?)", (today_str, 'user002', '품질1팀'))
cursor.execute("INSERT INTO schedules (scheduleDate, employeeId, teamName) VALUES (?, ?, ?)", (today_str, 'user005', '품질1팀'))
cursor.execute("INSERT INTO schedules (scheduleDate, employeeId, teamName) VALUES (?, ?, ?)", (today_str, 'user008', '품질1팀'))
cursor.execute("INSERT INTO schedules (scheduleDate, employeeId, teamName) VALUES (?, ?, ?)", (today_str, 'user006', '안전2팀'))
cursor.execute("INSERT INTO schedules (scheduleDate, employeeId, teamName) VALUES (?, ?, ?)", (today_str, 'user007', '안전2팀'))

# 근무 기록 데이터
cursor.execute("INSERT INTO logs (employeeName, date, checkInTime, safetyCheck, source) VALUES (?, ?, ?, ?, ?)",
               ('홍길동', today_str, now_time_str, '양호', 'system'))
cursor.execute("INSERT INTO logs (employeeName, date, checkInTime, safetyCheck, source) VALUES (?, ?, ?, ?, ?)",
               ('윤도현', today_str, now_time_str, '불량', 'system'))
               
# 공지사항 데이터
cursor.execute("INSERT INTO notices (title, content, createdAt) VALUES (?, ?, ?)",
               ('전체 공지: 안전 장비 착용 필수 안내', '모든 현장 인원은 안전모 및 안전화를 반드시 착용해 주시기 바랍니다.', today_str))

connection.commit()
connection.close()

print("데이터베이스가 성공적으로 초기화되었습니다. 모든 기능이 정상적으로 적용되었습니다.")

