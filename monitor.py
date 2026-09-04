# -*- coding: utf-8 -*-
"""
서울형 키즈카페 예약 알리미 (클라우드 버전)
--------------------------------------------
지정된 서울형 키즈카페 예약 캘린더 페이지를 주기적으로 확인하여,
마감(잔여 0석)되어 있던 날짜/회차가 예약가능(1석 이상)으로 바뀌면
텔레그램으로 즉시 알립니다.

브라우저가 필요 없는 순수 HTTP 방식이라 가볍고 빠릅니다.
GitHub Actions가 이 스크립트를 주기적으로 대신 실행해 주므로
PC나 휴대폰을 켜 둘 필요가 없습니다.

동작 방식:
  1. 캘린더 페이지를 그대로 받아옵니다.
  2. 달력의 각 날짜 칸(<td>)에서 회차별 "잔여 인원" 숫자를 읽습니다.
  3. 이전 실행 때 저장해 둔 state.json 과 비교합니다.
  4. 이전에 0(마감)이었거나 아예 없던 회차가 이번에 1 이상이 되면 "새로 열림"으로 보고 알림을 보냅니다.
  5. 이번 결과를 state.json 에 다시 저장합니다(다음 실행 때 비교용).

주의: 처음 실행할 때는 비교 대상이 없으므로 알림을 보내지 않고
      현재 상태만 저장합니다(그래야 첫 실행에 이미 열려있는 자리까지
      전부 알림이 쏟아지는 것을 막을 수 있습니다).
"""

import os
import re
import sys
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup

# ==========================================================================
# [설정] — 감시할 대상과 알림 정보
# ==========================================================================

TARGET_URL = os.environ.get(
    "TARGET_URL",
    "https://umppa.seoul.go.kr/icare/user/kidsCafeResve/BD_selectKidsCafeResveCal.do?q_fcltyId=YF260101&q_fcltyStle=2001",
)

# 텔레그램 알림 (GitHub Actions Secrets 로 주입됩니다. 로컬 테스트 시에는
# 환경변수로 직접 넣거나 아래 기본값을 채워서 테스트하세요.)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# ==========================================================================
# 여기서부터는 건드리지 않아도 됩니다.
# ==========================================================================

KST = timezone(timedelta(hours=9))
WEEK = "월화수목금토일"


def now_str():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print("[{}] {}".format(now_str(), msg), flush=True)


def notify_telegram(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        log("텔레그램 토큰/Chat ID가 설정되지 않아 알림을 보내지 못했습니다.")
        return
    url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_BOT_TOKEN)
    data = urllib.parse.urlencode(
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            r.read()
        log("텔레그램 알림 전송 완료")
    except Exception as e:
        log("텔레그램 전송 실패: {}".format(e))


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_calendar(html):
    """캘린더 HTML을 읽어 {날짜: {회차구분: 잔여인원}} 형태로 반환."""
    soup = BeautifulSoup(html, "html.parser")

    month_span = soup.select_one("div.calendar div.month span")
    m = re.match(r"(\d{4})\.\s*(\d{1,2})", month_span.get_text(strip=True)) if month_span else None
    if not m:
        raise RuntimeError("연/월 헤더를 찾지 못했습니다(페이지 구조가 바뀌었을 수 있습니다)")
    year, month = int(m.group(1)), int(m.group(2))

    cal_table = soup.select_one("div.calendar table")
    if not cal_table:
        raise RuntimeError("달력 테이블을 찾지 못했습니다(페이지 구조가 바뀌었을 수 있습니다)")

    state = {}
    for td in cal_table.select("td"):
        title = td.get("title", "")
        tm = re.match(r"(\d{1,2})일\s*(예약가능|예약불가)", title)
        if not tm:
            continue
        day = int(tm.group(1))
        try:
            date = datetime(year, month, day).date()
        except ValueError:
            continue

        rounds = {}
        for p in td.select("div > p"):
            u = p.select_one("u")
            i = p.select_one("i")
            if not (u and i):
                continue
            round_txt = p.get_text(" ", strip=True)
            rm = re.match(r"(\d+회)", round_txt)
            round_no = rm.group(1) if rm else "?"
            key = "{} {}".format(round_no, u.get_text(strip=True))
            try:
                rounds[key] = int(i.get_text(strip=True))
            except ValueError:
                pass

        state[date.isoformat()] = rounds

    if not state:
        raise RuntimeError("달력에서 날짜 정보를 하나도 읽지 못했습니다(페이지 구조 확인 필요)")

    return state


def find_newly_opened(prev, cur):
    """prev/cur: {날짜: {회차구분: 잔여인원}}. 새로 열린 (날짜, 회차구분, 잔여인원) 목록 반환."""
    opened = []
    for date, rounds in cur.items():
        prev_rounds = prev.get(date, {})
        for round_key, count in rounds.items():
            if count <= 0:
                continue
            prev_count = prev_rounds.get(round_key, 0)
            if prev_count <= 0:
                opened.append((date, round_key, count))
    return opened


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("slots")
        except Exception as e:
            log("이전 state.json 을 읽는 데 실패했습니다: {}".format(e))
            return None
    return None


def save_state(slots):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"last_checked": now_str(), "slots": slots},
            f,
            ensure_ascii=False,
            indent=2,
        )


def main():
    log("=" * 60)
    log("서울형 키즈카페 예약 감시")
    log("대상: {}".format(TARGET_URL))

    try:
        html = fetch_html(TARGET_URL)
        cur = parse_calendar(html)
    except Exception as e:
        log("확인 중 오류 발생: {}".format(e))
        save_state({"__error__": str(e)})
        sys.exit(0)  # 워크플로 자체는 실패로 남기지 않음(다음 주기에 재시도)

    prev = load_state()

    if prev is None:
        available_now = [
            (d, k, c) for d, rounds in cur.items() for k, c in rounds.items() if c > 0
        ]
        log("최초 실행입니다. 알림 없이 현재 상태만 저장합니다.")
        if available_now:
            log(
                "(참고) 현재 예약 가능: "
                + ", ".join("{} {}({}석)".format(d, k, c) for d, k, c in sorted(available_now))
            )
        else:
            log("(참고) 현재 예약 가능한 자리 없음")
    else:
        opened = find_newly_opened(prev, cur)
        if opened:
            lines = []
            for d, k, c in sorted(opened):
                dow = WEEK[datetime.strptime(d, "%Y-%m-%d").weekday()]
                lines.append("• {}({}) {} — {}자리".format(d, dow, k, c))
            text = (
                "🎉 키즈카페 예약 가능한 자리가 생겼습니다!\n\n"
                + "\n".join(lines)
                + "\n\n예약: {}".format(TARGET_URL)
            )
            log("★★★ 새로 열린 자리 발견!\n" + text)
            notify_telegram(text)
        else:
            log("변화 없음 (새로 열린 자리 없음)")

    save_state(cur)
    log("완료")


if __name__ == "__main__":
    main()
