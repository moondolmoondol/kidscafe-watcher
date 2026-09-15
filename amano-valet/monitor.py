# -*- coding: utf-8 -*-
"""
아마노 주차대행 예약 알리미 (+ 자동예약)
--------------------------
인천공항 아마노 주차대행 예약 페이지에서, 지정한 날짜의 "당일 접수가능 대수"가
마감(false) 상태였다가 예약가능(true)으로 바뀌는 순간, 설정된 정보로 곧바로
예약을 접수하고 텔레그램으로 결과를 알립니다.

이 사이트는 예약 화면에서 날짜를 고를 때 아래 API를 호출해서 그 날짜가
꽉 찼는지 확인합니다. 이 스크립트는 브라우저 없이 그 API를 직접 호출합니다.

  GET https://api.amanopark.co.kr/api/web/setting/booking/check?date=YYYY-MM-DD&type=BASIC
  응답 예) {"result":{"code":200,"message":"성공"},"data":false}
  data가 false면 마감(예약 불가), true면 예약 가능.

예약 자체는 아래 API로 접수합니다 (결제는 온라인이 아니라 현장에서 진행되므로
결제 정보는 필요 없습니다):

  POST https://api.amanopark.co.kr/api/web/booking/reservation
  body: {name, phone, carType, carNumber, carModel, carColor, carBrand, type,
         arrivedAt, departingAt, customerRequest, root:"WEB", isUsingCarWash,
         isCrew, carWashType, departingAir}

여러 날짜를 동시에 감시하고 싶으면 TARGET_DATES 에 쉼표로 구분해 추가하세요.
단, 자동예약(AUTO_BOOK)은 예약 정보(날짜/시간/항공편 등)가 고정되어 있으므로
날짜를 하나만 감시할 때 사용하는 것을 전제로 합니다.
"""

import os
import json
import urllib.request
import urllib.parse
import urllib.error
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
RESERVATION_API_URL = "https://api.amanopark.co.kr/api/web/booking/reservation"

# 자동예약 여부. false로 두면 예전처럼 알림만 보냅니다.
AUTO_BOOK = os.environ.get("AUTO_BOOK", "true").lower() == "true"

# 예약에 사용할 개인/차량/일정 정보 (모두 GitHub Secrets 로 주입)
CUSTOMER_NAME = os.environ.get("CUSTOMER_NAME", "")
CUSTOMER_PHONE = os.environ.get("CUSTOMER_PHONE", "")  # '-' 없이 숫자만, 010으로 시작 11자리
CAR_NUMBER = os.environ.get("CAR_NUMBER", "")  # 예: 387주9695
CAR_MODEL = os.environ.get("CAR_MODEL") or None
CAR_BRAND = os.environ.get("CAR_BRAND") or None  # codes.data.valet.carBrand 의 코드 (예: 볼보=VO)
CAR_COLOR = os.environ.get("CAR_COLOR") or None  # codes.data.valet.carColor 의 코드
CAR_TYPE = os.environ.get("CAR_TYPE", "BASIC")  # 할인 유형: BASIC(일반)만 온라인 예약 가능
DEPARTING_AT = os.environ.get("DEPARTING_AT", "")  # "YYYY-MM-DD HH:mm" (출발/차량 위탁 일시, 30분 단위)
ARRIVED_AT = os.environ.get("ARRIVED_AT", "")  # "YYYY-MM-DD HH:mm" (귀국/차량 인수 일시, 30분 단위)
DEPARTING_AIR = os.environ.get("DEPARTING_AIR", "")  # codes.data.booking.departingAir 의 코드 (예: 진에어=LJ)
CUSTOMER_REQUEST = os.environ.get("CUSTOMER_REQUEST") or None
IS_USING_CAR_WASH = os.environ.get("IS_USING_CAR_WASH", "false").lower() == "true"
CAR_WASH_TYPE = os.environ.get("CAR_WASH_TYPE") or None  # IN/OUT/ALL (세차 서비스 이용 시 필수)

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


def missing_booking_config():
    """예약에 반드시 필요한 값 중 비어있는 항목의 이름 목록을 돌려줍니다.
    GitHub Secrets 이름을 잘못 등록하면 os.environ.get()이 조용히 빈 문자열을
    돌려주기 때문에, 잘못된 값으로 예약이 나가버리는 것을 막기 위한 안전장치."""
    required = {
        "CUSTOMER_NAME": CUSTOMER_NAME,
        "CUSTOMER_PHONE": CUSTOMER_PHONE,
        "CAR_NUMBER": CAR_NUMBER,
        "DEPARTING_AT": DEPARTING_AT,
        "ARRIVED_AT": ARRIVED_AT,
        "DEPARTING_AIR": DEPARTING_AIR,
    }
    return [name for name, value in required.items() if not value]


def book_reservation():
    """설정된 정보로 실제 예약을 접수하고 (성공 여부, 응답 본문/에러메시지)를 반환합니다."""
    data = {
        "name": CUSTOMER_NAME,
        "phone": CUSTOMER_PHONE,
        "carType": CAR_TYPE,
        "carNumber": CAR_NUMBER,
        "carModel": CAR_MODEL,
        "carColor": CAR_COLOR,
        "carBrand": CAR_BRAND,
        "type": SERVICE_TYPE,
        "arrivedAt": ARRIVED_AT,
        "departingAt": DEPARTING_AT,
        "customerRequest": CUSTOMER_REQUEST,
        "root": "WEB",
        "isUsingCarWash": IS_USING_CAR_WASH,
        "isCrew": False,
        "carWashType": CAR_WASH_TYPE,
        "departingAir": DEPARTING_AIR,
    }
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        RESERVATION_API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": BOOKING_PAGE_URL,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode("utf-8"))
        # 사이트 프론트엔드 로직과 동일하게, result.code==200 은 API 호출 자체가
        # 정상 처리됐다는 뜻일 뿐이고, 실제 예약 성사 여부는 응답의 data 필드가
        # (uid 등으로) 채워져 있는지로 판단해야 한다. data 가 null/false 면 예약은
        # 거절된 것이다 (경쟁 상황으로 자리가 이미 소진된 경우 등).
        ok = bool(resp.get("data"))
        return ok, resp
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {"message": str(e)}
        return False, err_body
    except Exception as e:
        return False, {"message": str(e)}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("dates"), data.get("booked", False)
        except Exception as e:
            log("이전 state.json 을 읽는 데 실패했습니다: {}".format(e))
            return None, False
    return None, False


def save_state(dates, booked):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"last_checked": now_str(), "dates": dates, "booked": booked},
            f,
            ensure_ascii=False,
            indent=2,
        )


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

    prev, booked = load_state()

    if prev is None:
        log("최초 실행입니다. 알림 없이 현재 상태만 저장합니다.")
        for d, ok in cur.items():
            dow = WEEK[datetime.strptime(d, "%Y-%m-%d").weekday()]
            log("(참고) {}({}) : {}".format(d, dow, "예약가능" if ok else "마감"))
    elif booked:
        log("이미 예약을 완료한 상태입니다. 추가 조치 없이 종료합니다.")
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
            opened_text = "\n".join(lines)
            log("★★★ 새로 열림!\n" + opened_text)

            missing = missing_booking_config() if AUTO_BOOK else []
            if AUTO_BOOK and missing:
                text = (
                    "🚨 아마노 주차대행 예약이 가능해졌는데 자동예약 설정이 불완전해서 시도하지 못했습니다!\n\n"
                    + opened_text
                    + "\n\n비어있는 값: {}\n(GitHub Secrets 이름이 잘못 등록됐을 수 있습니다)\n\n"
                    "서둘러 직접 예약해 주세요: {}".format(", ".join(missing), BOOKING_PAGE_URL)
                )
                log("자동예약 설정 누락으로 건너뜀: {}".format(missing))
            elif AUTO_BOOK:
                log("자동예약을 시도합니다...")
                ok, resp = book_reservation()
                if ok:
                    booked = True
                    text = (
                        "🚗 아마노 주차대행 예약이 가능해져서 자동으로 예약을 접수했습니다!\n\n"
                        + opened_text
                        + "\n\n성명: {}\n차량번호: {}\n출발: {}\n귀국: {}\n예약 데이터: {}\n\n"
                        "반드시 예약확인 페이지에서 실제로 등록됐는지 확인해 주세요: {}/booking-check".format(
                            CUSTOMER_NAME,
                            CAR_NUMBER,
                            DEPARTING_AT,
                            ARRIVED_AT,
                            resp.get("data"),
                            BOOKING_PAGE_URL.rsplit("/", 1)[0],
                        )
                    )
                    log("자동예약 성공: {}".format(resp))
                else:
                    # 예약이 거절된 경우이므로 booked 플래그를 세우지 않는다.
                    # (다음 번 자리가 다시 열리면 재시도할 수 있도록)
                    text = (
                        "⚠️ 아마노 주차대행 예약이 가능해졌지만 자동예약에 실패했습니다!\n\n"
                        + opened_text
                        + "\n\n서버 응답: {}\n\n서둘러 직접 예약해 주세요: {}".format(resp, BOOKING_PAGE_URL)
                    )
                    log("자동예약 실패: {}".format(resp))
            else:
                text = (
                    "🚗 아마노 주차대행 예약이 가능해졌습니다!\n\n"
                    + opened_text
                    + "\n\n예약: {}".format(BOOKING_PAGE_URL)
                )

            notify_telegram(text)
        else:
            log("변화 없음 (여전히 마감 또는 이미 알림 보낸 상태 유지)")

    save_state(cur, booked)
    log("완료")


if __name__ == "__main__":
    main()
