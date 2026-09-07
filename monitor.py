# -*- coding: utf-8 -*-
"""
서울형 키즈카페 예약 알리미 (클라우드 버전)
--------------------------------------------
지정된 서울형 키즈카페의 "키즈카페(놀이시설) 예약"과 "프로그램(체험 활동) 예약"
두 가지를 모두 확인하여, 마감(잔여 0석)되어 있던 날짜/회차/프로그램이
예약가능(1석 이상)으로 바뀌면 텔레그램으로 즉시 알립니다.

브라우저가 필요 없는 순수 HTTP 방식이라 가볍고 빠릅니다.
GitHub Actions가 이 스크립트를 주기적으로 대신 실행해 주므로
PC나 휴대폰을 켜 둘 필요가 없습니다.

동작 방식:
  1. 캘린더 페이지를 받아와서, 예약 가능(활성화)한 날짜 목록을 읽습니다.
  2. 각 날짜마다 사이트 내부 API(ND_selectResveTmeList.do)를 그대로 호출해서
     "키즈카페" 회차별 잔여 인원과, "프로그램" 각 항목별 잔여 인원을 받아옵니다.
     (달력 화면에서 날짜를 클릭했을 때 사이트가 실제로 호출하는 것과 동일한 API입니다.)
  3. 이전 실행 때 저장해 둔 state.json 과 비교합니다.
  4. 이전에 0(마감)이었거나 아예 없던 항목이 이번에 1 이상이 되면
     "새로 열림"으로 보고 알림을 보냅니다.
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

FCLTY_ID = os.environ.get("FCLTY_ID", "YF260101")
FCLTY_STLE = os.environ.get("FCLTY_STLE", "2001")

CAL_URL = (
    "https://umppa.seoul.go.kr/icare/user/kidsCafeResve/BD_selectKidsCafeResveCal.do"
    "?q_fcltyId={}&q_fcltyStle={}".format(FCLTY_ID, FCLTY_STLE)
)
DETAIL_URL = "https://umppa.seoul.go.kr/icare/user/kidsCafeResve/ND_selectResveTmeList.do"

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

HEADERS_COMMON = {
    "User-Agent": "Mozilla/5.0",
    "Referer": CAL_URL,
}


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
    req = urllib.request.Request(url, headers=HEADERS_COMMON)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def day_no(date):
    """사이트가 쓰는 요일번호: 일=1, 월=2, ..., 토=7."""
    return (date.isoweekday() % 7) + 1


def bookable_dates(html):
    """캘린더 HTML에서 '예약가능'으로 표시된 날짜 목록을 반환."""
    soup = BeautifulSoup(html, "html.parser")

    month_span = soup.select_one("div.calendar div.month span")
    m = re.match(r"(\d{4})\.\s*(\d{1,2})", month_span.get_text(strip=True)) if month_span else None
    if not m:
        raise RuntimeError("연/월 헤더를 찾지 못했습니다(페이지 구조가 바뀌었을 수 있습니다)")
    year, month = int(m.group(1)), int(m.group(2))

    cal_table = soup.select_one("div.calendar table")
    if not cal_table:
        raise RuntimeError("달력 테이블을 찾지 못했습니다(페이지 구조가 바뀌었을 수 있습니다)")

    dates = []
    for td in cal_table.select("td"):
        title = td.get("title", "")
        tm = re.match(r"(\d{1,2})일\s*예약가능", title)
        if not tm:
            continue
        day = int(tm.group(1))
        try:
            dates.append(datetime(year, month, day).date())
        except ValueError:
            continue

    if not dates:
        raise RuntimeError("예약 가능한 날짜를 하나도 찾지 못했습니다(페이지 구조 확인 필요)")

    return dates


def fetch_detail(date):
    """해당 날짜의 키즈카페 회차별/프로그램별 잔여 인원을 {항목명: 잔여인원} 형태로 반환."""
    body = urllib.parse.urlencode(
        {
            "q_fcltyId": FCLTY_ID,
            "q_resveDe": date.isoformat(),
            "q_dayNo": day_no(date),
        }
    ).encode("utf-8")
    headers = dict(HEADERS_COMMON)
    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    req = urllib.request.Request(DETAIL_URL, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        payload = json.loads(r.read().decode("utf-8"))

    value = payload.get("value") or {}
    items = {}

    for t in value.get("tmeData") or []:
        # resvePsncpa = 이 회차에 배정된 예약 가능 정원(전체 시설 정원인 usePsncpa와 다름)
        # resveNmpr   = 그중 이미 예약된 인원
        remain = (t.get("resvePsncpa") or 0) - (t.get("resveNmpr") or 0)
        key = "[키즈카페] {}회 {}".format(t.get("tmeSn"), t.get("tmeSeNm") or "")
        items[key] = max(remain, 0)

    for p in value.get("progrmData") or []:
        # posUserNmpr 자체가 이미 "잔여 가능 인원" 값입니다.
        remain = p.get("posUserNmpr") or 0
        begin = p.get("progrmBeginTime") or ""
        end = p.get("progrmEndTime") or ""
        name = (p.get("progrmNm") or "").strip()
        key = "[프로그램] {}~{} {}".format(begin, end, name)
        items[key] = max(remain, 0)

    return items


def find_newly_opened(prev, cur):
    """prev/cur: {날짜: {항목명: 잔여인원}}. 새로 열린 (날짜, 항목명, 잔여인원) 목록 반환."""
    opened = []
    for date, items in cur.items():
        prev_items = prev.get(date, {})
        for key, count in items.items():
            if count <= 0:
                continue
            prev_count = prev_items.get(key, 0)
            if prev_count <= 0:
                opened.append((date, key, count))
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
    log("서울형 키즈카페 예약 감시 (키즈카페 + 프로그램)")
    log("대상: {}".format(CAL_URL))

    cur = {}
    try:
        html = fetch_html(CAL_URL)
        dates = bookable_dates(html)
        log("예약 가능 날짜 {}개 확인, 상세 조회 중...".format(len(dates)))
        for d in dates:
            try:
                cur[d.isoformat()] = fetch_detail(d)
            except Exception as e:
                log("{} 상세 조회 중 오류(건너뜀): {}".format(d.isoformat(), e))
    except Exception as e:
        log("확인 중 오류 발생: {}".format(e))
        save_state({"__error__": str(e)})
        sys.exit(0)  # 워크플로 자체는 실패로 남기지 않음(다음 주기에 재시도)

    if not cur:
        log("가져온 상세 데이터가 없어 이번 실행을 종료합니다.")
        sys.exit(0)

    prev = load_state()

    if prev is None:
        available_now = [(d, k, c) for d, items in cur.items() for k, c in items.items() if c > 0]
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
                "🎉 예약 가능한 자리가 생겼습니다!\n\n"
                + "\n".join(lines)
                + "\n\n예약: {}".format(CAL_URL)
            )
            log("★★★ 새로 열린 자리 발견!\n" + text)
            notify_telegram(text)
        else:
            log("변화 없음 (새로 열린 자리 없음)")

    save_state(cur)
    log("완료")


if __name__ == "__main__":
    main()
