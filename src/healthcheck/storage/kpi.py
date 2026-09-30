"""교육 회차별 KPI 계산. 응답 기록(events)만으로 계산하는 순수 함수다.

참여자는 "교육 시간 안에 처음 쓴 사람"이다. 팀원은 교육 전부터 써 왔으므로 자동으로 빠진다.
응답 기록이 생기기 전에 쓴 사람도 빠지도록 판정 기록의 사용자(prior_first_seen)를 함께 본다.

용어
  확인 요청  어르신이 직접 보낸 링크·문장에 대한 답(kind=check, [한번 해 보기] 예시 제외)
  성공      확인 요청에 믿을 만해요 · 조심하세요 · 주의하세요 중 하나가 나간 것
  막힌 응답  확인 요청에 성공이 아닌 답(알 수 없어요 · 범위 밖 · 안내 · 잠시 뒤 다시 · 실패)이 나갔거나 사진을 보낸 것
  행동      어르신이 보낸 메시지 하나(버튼 포함). 채널 진입 인사와 [한번 해 보기]가 대신 보낸 예시 확인은 뺀다
날짜는 한국 시간으로 센다.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median

from healthcheck.ports.events import Event

KST = timezone(timedelta(hours=9))
SUCCESS = frozenset({"TRUSTWORTHY", "CAUTION", "WARNING"})
# 결과 카드(버튼 달린 신호등 카드)가 나간 답
CARD = SUCCESS | {"UNKNOWN"}
REUSE_WINDOWS = {"3일 뒤": (3, 6), "1주 뒤": (7, 13)}
CHURN_MINUTES = 30


def is_action(event: Event) -> bool:
    if event.kind == "check" and event.via == "try":
        return False
    if event.kind == "welcome" and event.outcome == "intent":
        return False
    return True


def is_submission(event: Event) -> bool:
    return event.kind == "check" and event.via != "try"


def is_success(event: Event) -> bool:
    return is_submission(event) and event.outcome in SUCCESS


def is_blocked(event: Event) -> bool:
    if event.kind == "media":
        return True
    return is_submission(event) and event.outcome not in SUCCESS


def kst_date(moment: datetime) -> date:
    return moment.astimezone(KST).date()


@dataclass(frozen=True, slots=True)
class Cohort:
    start: datetime | None = None
    end: datetime | None = None

    def contains(self, moment: datetime) -> bool:
        if self.start and moment < self.start:
            return False
        if self.end and moment > self.end:
            return False
        return True


def parse_cohort(text: str | None) -> Cohort:
    """ "2026-10-05T09:00~2026-10-06T18:00" 또는 "2026-10-05~2026-10-06"(끝 날짜 하루 전체 포함). 한국 시간."""
    if not text:
        return Cohort()
    left, _, right = text.partition("~")

    def at(value: str, *, end: bool) -> datetime:
        value = value.strip()
        if "T" in value or " " in value:
            moment = datetime.fromisoformat(value.replace(" ", "T"))
        else:
            day = date.fromisoformat(value)
            moment = datetime.combine(day, datetime.max.time() if end else datetime.min.time())
        return moment.replace(tzinfo=KST) if moment.tzinfo is None else moment

    start = at(left, end=False)
    return Cohort(start=start, end=at(right, end=True) if right.strip() else None)


def compute(
    events: list[Event],
    *,
    cohort: Cohort = Cohort(),
    prior_first_seen: dict[str, datetime] | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    by_user: dict[str, list[Event]] = defaultdict(list)
    for event in sorted(events, key=lambda e: e.created_at):
        if event.user_hash:
            by_user[event.user_hash].append(event)

    first_seen: dict[str, datetime] = {}
    for user, items in by_user.items():
        first = items[0].created_at
        prior = (prior_first_seen or {}).get(user)
        first_seen[user] = min(first, prior) if prior else first
    participants = sorted(user for user in by_user if cohort.contains(first_seen[user]))

    report: dict = {"participants": len(participants), "cohort": cohort}
    # 포스터 스캔은 누가 찍었는지 모른다. 교육 기간 안의 스캔을 장소 태그별로 센다.
    scans = [e for e in events if e.kind == "poster_scan" and cohort.contains(e.created_at)]
    report["poster"] = {
        "scans": len(scans),
        "by_tag": Counter(e.outcome for e in scans),
        "by_day": Counter(kst_date(e.created_at).isoformat() for e in scans),
    }
    if not participants:
        return report

    actions = {user: [e for e in by_user[user] if is_action(e)] for user in participants}
    all_events = [e for user in participants for e in by_user[user]]
    submissions = [e for e in all_events if is_submission(e)]

    # ① 혼자 확인 성공
    succeeded = [user for user in participants if any(is_success(e) for e in by_user[user])]
    report["success"] = {"users": len(succeeded)}

    # ② 첫 성공까지 걸린 단계
    steps: list[int] = []
    paths: Counter = Counter()
    never = 0
    for user in participants:
        sequence = actions[user]
        index = next((i for i, e in enumerate(sequence) if is_success(e)), None)
        if index is None:
            never += 1
            continue
        steps.append(index + 1)
        trail = [step_name(e) for e in sequence[: index + 1]]
        paths[" → ".join(trail if len(trail) <= 6 else trail[:5] + ["…", trail[-1]])] += 1
    report["steps"] = {
        "values": steps,
        "never": never,
        "distribution": Counter(min(value, 4) for value in steps),
        "paths": paths.most_common(5),
    }

    # ③ 재사용률
    today = kst_date(now)
    reuse = {}
    for name, (low, high) in REUSE_WINDOWS.items():
        measurable = [user for user in participants if (today - kst_date(first_seen[user])).days >= high]
        reused = [
            user
            for user in measurable
            if any(
                low <= (kst_date(e.created_at) - kst_date(first_seen[user])).days <= high
                for e in by_user[user]
                if is_submission(e)
            )
        ]
        reuse[name] = {"measurable": len(measurable), "reused": len(reused), "window": (low, high)}
    report["reuse"] = reuse

    # ④ 신호등과 입력
    report["labels"] = Counter(e.outcome for e in submissions if e.outcome in CARD)
    report["try_checks"] = sum(1 for e in all_events if e.kind == "check" and e.via == "try")
    report["inputs"] = {
        "kind": Counter(e.input_kind for e in submissions if e.input_kind),
        "form": Counter(e.input_form for e in submissions if e.input_form),
        "site": Counter(e.site for e in submissions if e.site).most_common(8),
        "media": sum(1 for e in all_events if e.kind == "media"),
    }

    # ⑤ 막힌 응답과 이탈
    blocked = [e for e in all_events if is_blocked(e)]
    denominator = len(submissions) + report["inputs"]["media"]
    left_30 = left_all = pending = 0
    for event in blocked:
        later = [e for e in actions.get(event.user_hash, []) if e.created_at > event.created_at]
        if not later or later[0].created_at - event.created_at > timedelta(minutes=CHURN_MINUTES):
            left_30 += 1
        if not later:
            if now - event.created_at >= timedelta(hours=24):
                left_all += 1
            else:
                pending += 1
    report["blocked"] = {
        "count": len(blocked),
        "denominator": denominator,
        "by_outcome": Counter(event.outcome if event.kind == "check" else "media" for event in blocked),
        "left_30m": left_30,
        "left_for_good": left_all,
        "pending": pending,
    }

    # ⑥ 결과 도착 시간
    latencies = sorted(e.latency_ms for e in all_events if e.kind == "check" and e.latency_ms is not None)
    report["latency"] = {
        "values": latencies,
        "undelivered": sum(1 for e in all_events if e.kind == "check" and e.delivered is False),
    }

    # ⑦ 근거 보기 · 친구 공유
    cards = [e for e in all_events if e.kind == "check" and e.outcome in CARD]
    card_users = {e.user_hash for e in cards}
    for kind in ("reason", "share"):
        clicks = [e for e in all_events if e.kind == kind and e.outcome in CARD]
        report[kind] = {
            "clicks": len(clicks),
            "cards": len(cards),
            "users": len({e.user_hash for e in clicks}),
            "card_users": len(card_users),
            "by_label": Counter(e.outcome for e in clicks),
        }

    # 공유된 카드가 열린 수. 사진을 누른 사람은 모르므로 카드(ref)로 이어 본다.
    owners = {e.event_id: e.user_hash for e in cards}
    opens = [e for e in events if e.kind == "share_open" and e.ref in owners]
    sent = [e for e in events if e.kind == "share_sent" and e.ref in owners]
    report["share_sent"] = {
        "sent": len(sent),
        "cards": len({e.ref for e in sent}),
        "senders": len({owners[e.ref] for e in sent}),
        "rooms": len({e.site for e in sent if e.site}),
        "by_chat": Counter(e.outcome for e in sent if e.outcome),
    }
    report["share_open"] = {
        "opens": len(opens),
        "by_target": Counter(e.outcome or "chat" for e in opens),
        "cards_opened": len({e.ref for e in opens}),
        "cards": len(cards),
        "senders": len({owners[e.ref] for e in opens}),
    }

    # ⑧ 안내 버튼 · 이용량
    report["guide"] = {
        kind: len({user for user in participants if any(e.kind == kind for e in by_user[user])})
        for kind in ("guide", "try", "caution")
    }
    report["guide"]["any"] = len(
        {user for user in participants if any(e.kind in ("guide", "try", "caution") for e in by_user[user])}
    )
    per_user = [sum(1 for e in by_user[user] if is_submission(e)) for user in participants]
    daily_actions, daily_checks = [], []
    for user in participants:
        days = {kst_date(e.created_at) for e in actions[user]}
        if not days:
            continue
        daily_actions.append(len(actions[user]) / len(days))
        daily_checks.append(sum(1 for e in actions[user] if is_submission(e)) / len(days))
    report["usage"] = {"checks_per_user": per_user, "daily_actions": daily_actions, "daily_checks": daily_checks}
    return report


STEP_NAMES = {
    "guide": "사용법",
    "caution": "주의사항",
    "try": "한번 해 보기",
    "welcome": "처음 인사",
    "small_talk": "짧은 말",
    "media": "사진",
    "reason": "왜 그런지",
    "share": "친구에게",
}
OUTCOME_NAMES = {
    "TRUSTWORTHY": "믿을 만해요",
    "CAUTION": "조심하세요",
    "WARNING": "주의하세요",
    "UNKNOWN": "알 수 없어요",
    "out_of_scope": "범위 밖(약·시술)",
    "notice:not_target": "확인 대상 아님",
    "notice:no_claim": "주장 없음",
    "notice:youtube_not_video": "영상 아닌 유튜브 링크",
    "notice:login_required": "로그인 필요한 사이트",
    "retry": "잠시 뒤 다시",
    "error": "처리 실패",
    "busy": "요청 많음",
    "no_callback": "콜백 없음",
    "media": "사진",
}
CHAT_NAMES = {
    "MemoChat": "나와의 채팅",
    "DirectChat": "1:1 채팅",
    "MultiChat": "단체 채팅",
    "OpenDirectChat": "오픈 1:1",
    "OpenMultiChat": "오픈 단체",
}
FORM_NAMES = {
    "video": "유튜브 영상",
    "shorts": "유튜브 쇼츠",
    "youtube_other": "유튜브(영상 아님)",
    "link": "기사·블로그 링크",
    "short_link": "단축 링크",
    "short_text": "짧은 문장(30자 미만)",
    "text": "문장(150자 미만)",
    "long_text": "긴 글",
}


def step_name(event: Event) -> str:
    if event.kind == "check":
        return OUTCOME_NAMES.get(event.outcome or "", event.outcome or "확인")
    return STEP_NAMES.get(event.kind, event.kind)


def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "-"


def _form_name(form: str) -> str:
    base, plus, _ = form.partition("+")
    return FORM_NAMES.get(base, base) + (" + 글 섞임" if plus else "")


def format_report(report: dict) -> str:
    cohort: Cohort = report["cohort"]
    span = "전체 기간"
    if cohort.start or cohort.end:
        fmt = lambda m: m.astimezone(KST).strftime("%m/%d %H:%M") if m else "…"
        span = f"{fmt(cohort.start)} ~ {fmt(cohort.end)} (한국 시간)"
    n = report["participants"]
    lines = [f"■ 참여자 수 {n}명 · {span}", "  (이 기간에 처음 쓴 사람. 그 전부터 쓰던 팀원은 빠진다)"]
    poster = report.get("poster") or {}
    lines += ["", f"■ 포스터 QR 스캔 {poster.get('scans', 0)}회"]
    if poster.get("by_tag"):
        lines.append("  " + " · ".join(f"{tag} {count}" for tag, count in poster["by_tag"].most_common()))
        lines.append("  " + " · ".join(f"{day} {count}" for day, count in sorted(poster["by_day"].items())))
    if not n:
        return "\n".join(lines)

    s = report["success"]["users"]
    lines += ["", f"■ 혼자 확인 성공률 {_pct(s, n)} ({s}/{n}명)", "  믿을 만해요·조심하세요·주의하세요를 한 번 이상 받은 사람"]

    st = report["steps"]
    values = st["values"]
    lines += ["", "■ 첫 제대로 된 확인까지 걸린 단계"]
    if values:
        first_try = st["distribution"].get(1, 0)
        lines.append(f"  평균 {mean(values):.1f}단계 · 중앙값 {median(values):g}단계 · 첫 시도 성공 {_pct(first_try, n)}")
        dist = st["distribution"]
        lines.append(
            f"  1단계 {dist.get(1, 0)}명 · 2단계 {dist.get(2, 0)}명 · 3단계 {dist.get(3, 0)}명 · 4단계 이상 {dist.get(4, 0)}명"
            f" · 끝내 못 함 {st['never']}명"
        )
        for path, count in st["paths"]:
            lines.append(f"  {count}명  {path}")
    else:
        lines.append(f"  아직 성공한 사람이 없다 (끝내 못 함 {st['never']}명)")

    lines += ["", "■ 재사용률"]
    for name, item in report["reuse"].items():
        low, high = item["window"]
        if item["measurable"]:
            lines.append(
                f"  {name} {_pct(item['reused'], item['measurable'])} ({item['reused']}/{item['measurable']}명,"
                f" 처음 쓴 날로부터 {low}~{high}일째에 다시 보냄)"
            )
        else:
            lines.append(f"  {name} 아직 잴 수 없다 (처음 쓴 날로부터 {high}일이 지나야 한다)")

    labels = report["labels"]
    lines += ["", "■ 신호등별 결과"]
    for key in ("TRUSTWORTHY", "CAUTION", "WARNING", "UNKNOWN"):
        lines.append(f"  {OUTCOME_NAMES[key]:<8} {labels.get(key, 0):>4}건")
    lines.append(f"  → 걸러 낸 과장·위험 정보 {labels.get('CAUTION', 0) + labels.get('WARNING', 0)}건")
    if report["try_checks"]:
        lines.append(f"  ([한번 해 보기] 예시 확인 {report['try_checks']}건은 위에 넣지 않았다)")

    inputs = report["inputs"]
    total_inputs = sum(inputs["kind"].values())
    lines += ["", f"■ 입력 종류 · 형태 (확인 요청 {total_inputs}건, 사진 {inputs['media']}건)"]
    for form, count in inputs["form"].most_common():
        lines.append(f"  {_form_name(form):<24} {count:>4}건 {_pct(count, total_inputs):>5}")
    if inputs["site"]:
        lines.append("  사이트: " + " · ".join(f"{site} {count}" for site, count in inputs["site"]))

    b = report["blocked"]
    lines += ["", f"■ 막힌 응답 {_pct(b['count'], b['denominator'])} ({b['count']}/{b['denominator']}건)"]
    for outcome, count in b["by_outcome"].most_common():
        lines.append(f"  {OUTCOME_NAMES.get(outcome, outcome):<16} {count:>4}건")
    if b["count"]:
        settled = b["count"] - b["pending"]
        lines.append(f"  막힌 뒤 {CHURN_MINUTES}분 안에 다시 보내지 않음 {_pct(b['left_30m'], b['count'])} ({b['left_30m']}건)")
        lines.append(
            f"  막힌 뒤 다시 오지 않음 {_pct(b['left_for_good'], settled)} ({b['left_for_good']}/{settled}건,"
            f" 24시간이 안 지난 {b['pending']}건은 제외)"
        )

    lat = report["latency"]["values"]
    lines += ["", "■ 결과 도착 시간"]
    if lat:
        p90 = lat[min(len(lat) - 1, int(len(lat) * 0.9))]
        lines.append(f"  중앙값 {median(lat) / 1000:.1f}초 · 90% {p90 / 1000:.1f}초 · 가장 긴 값 {lat[-1] / 1000:.1f}초 ({len(lat)}건)")
    lines.append(f"  결과를 보내지 못한 건 {report['latency']['undelivered']}건")

    so = report["share_open"]
    lines += [
        "",
        f"■ 공유된 결과 카드가 열린 수 {so['opens']}회 · 결과 카드 {so['cards']}장 중 {so['cards_opened']}장"
        f" ({_pct(so['cards_opened'], so['cards'])}, 결과를 받은 사람 {so['senders']}명)",
        "  공유받은 메시지(또는 카드 사진)를 눌러 들어온 수다. 결과를 받은 본인이 누른 것도 섞일 수 있다",
    ]
    if so["by_target"]:
        names = {"chat": "나도 확인해 보기", "home": "채널 홈", "source": "원본 보기"}
        lines.append("  " + " · ".join(f"{names.get(k, k)} {v}" for k, v in so["by_target"].most_common()))

    for kind, title in (("reason", "근거 보기('왜 그런지 보기')"), ("share", "친구 공유 버튼('친구에게 보내기') 누름")):
        r = report[kind]
        lines += [
            "",
            f"■ {title} {r['clicks']}회 · 결과 카드 대비 {_pct(r['clicks'], r['cards'])}",
            f"  누른 사람 {r['users']}/{r['card_users']}명 ({_pct(r['users'], r['card_users'])})",
        ]
        if r["by_label"]:
            lines.append("  " + " · ".join(f"{OUTCOME_NAMES.get(k, k)} {v}" for k, v in r["by_label"].most_common()))
        if kind == "share":
            ss = report["share_sent"]
            lines.append(
                f"  실제로 보냄 {ss['sent']}회 · 카드 {ss['cards']}장 · 보낸 사람 {ss['senders']}명 · 서로 다른 채팅방 {ss['rooms']}곳"
            )
            if ss["by_chat"]:
                lines.append("  " + " · ".join(f"{CHAT_NAMES.get(k, k)} {v}" for k, v in ss["by_chat"].most_common()))
            if not r["clicks"] and not ss["sent"]:
                lines.append("  (KAKAO_JS_KEY를 설정하기 전에는 누른 수와 보낸 수를 셀 수 없다)")

    g = report["guide"]
    lines += [
        "",
        f"■ 안내 버튼을 누른 참여자 {_pct(g['any'], n)} ({g['any']}명)",
        f"  사용법 {g['guide']}명 · 한번 해 보기 {g['try']}명 · 주의사항 {g['caution']}명",
    ]

    u = report["usage"]
    lines += ["", "■ 이용량"]
    if u["checks_per_user"]:
        lines.append(f"  1인당 확인 요청 평균 {mean(u['checks_per_user']):.1f}건 · 중앙값 {median(u['checks_per_user']):g}건")
    if u["daily_actions"]:
        lines.append(
            f"  사용자당 하루 평균 이용 {mean(u['daily_actions']):.1f}회(버튼 포함)"
            f" · 확인 요청 {mean(u['daily_checks']):.1f}회 (쓴 날 기준)"
        )
    return "\n".join(lines)
