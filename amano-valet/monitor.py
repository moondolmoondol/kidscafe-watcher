# -*- coding: utf-8 -*-
"""
아마노 주차대행 예약 알리미
--------------------------
인천공항 아마노 주차대행 예약 페이지에서, 지정한 날짜의 "당일 접수가능 대수"가
마감(false) 상태였다가 예약가능(true)으로 바뀌는 순간 텔레그램으로 알립니다.

이 사이트는 예약 화면에서 날짜를 고를 때 아래 API를 호출해서 그 날짜가
꽉 찼는지 확인합니다. 이 스크립트는 브라우저 없이 그 API를 직접 호출합니다.

  GET https://api.amanopark.co.kr/api/web/setting/booking/check?date=YYYY-MM-DD&type=BASIC
  응답 예) {"result":{"code":200,"message":"성공"},"data":false}
  data가 false면 마감(예약 불가), true면 예약 가능.

여러 날짜를 동시에 감시하고 싶으면 TARGET_DATES 에 쉼표로 구분해 추가하세요.
"""

import os
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ==========================================================================
# [설정]
# ==========================================================================

# 감시할 날짜(들). 쉼표로 여러 개 지정 가능. 예) "2026-09-26,2026-09-27"
TARGET_DATES = [d.strip() for d in os.environ.get("TARGET_DATES", "2026-09-26").split(",") if d.strip()]

# 서비스 유형: BASIC(일반) / PREMIUM(프리미엄)
SERVICE_TYPE = os.environ.get("SERVICE_TYPE", "BASIC")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = os.environ.get("STATE_FILE", "state.json")

BOOKING_PAGE_URL = "https://valet.amanopark.co.kr/booking"
API_URL = "https://api.amanopark.co.kr/api/web/setting/booking/check"

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


def check_date(date_str):
    """해당 날짜가 예약 가능한지(True) / 마감(False)인지 서버에 물어봄."""
    qs = urllib.parse.urlencode({"date": date_str, "type": SERVICE_TYPE})
    url = "{}?{}".format(API_URL, qs)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": BOOKING_PAGE_URL,
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read().decode("utf-8"))
    return bool(body.get("data", False))


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("dates")
        except Exception as e:
            log("이전 state.json 을 읽는 데 실패했습니다: {}".format(e))
            return None
    return None


def save_state(dates):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_checked": now_str(), "dates": dates}, f, ensure_ascii=False, indent=2)


def main():
    log("=" * 60)
    log("아마노 주차대행 예약 감시 ({})".format(SERVICE_TYPE))
    log("감시 날짜: {}".format(", ".join(TARGET_DATES)))

    cur = {}
    for d in TARGET_DATES:
        try:
            cur[d] = check_date(d)
        except Exception as e:
            log("{} 확인 중 오류: {}".format(d, e))

    if not cur:
        log("확인된 날짜가 없어 이번 실행을 종료합니다.")
        return

    prev = load_state()

    if prev is None:
        log("최초 실행입니다. 알림 없이 현재 상태만 저장합니다.")
        for d, ok in cur.items():
            dow = WEEK[datetime.strptime(d, "%Y-%m-%d").weekday()]
            log("(참고) {}({}) : {}".format(d, dow, "예약가능" if ok else "마감"))
    else:
        opened = []
        for d, ok in cur.items():
            was_ok = prev.get(d, False)
            if ok and not was_ok:
                opened.append(d)

        if opened:
            lines = []
            for d in sorted(opened):
                dow = WEEK[datetime.strptime(d, "%Y-%m-%d").weekday()]
                lines.append("• {}({}) 예약 가능해짐".format(d, dow))
            text = (
                "🚗 아마노 주차대행 예약이 가능해졌습니다!\n\n"
                + "\n".join(lines)
                + "\n\n예약: {}".format(BOOKING_PAGE_URL)
            )
            log("★★★ 새로 열림!\n" + text)
            notify_telegram(text)
        else:
            log("변화 없음 (여전히 마감 또는 이미 알림 보낸 상태 유지)")

    save_state(cur)
    log("완료")


if __name__ == "__main__":
    main()
