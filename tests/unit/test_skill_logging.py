"""카카오 요청 본문 로그의 식별자 가림."""

from healthcheck.api.skill import mask_body


def test_실채널_요청의_스네이크_표기_식별자도_가린다():
    # 실제 채널 요청에서 확인한 properties 구조다. 값은 가짜다.
    body = {
        "userRequest": {
            "user": {
                "id": "u-1",
                "type": "botUserKey",
                "properties": {
                    "botUserKey": "k-1",
                    "isFriend": True,
                    "plusfriendUserKey": "p-1",
                    "bot_user_key": "k-1",
                    "plusfriend_user_key": "p-1",
                    "app_user_id": "a-1",
                },
            },
            "utterance": "홍삼이 면역력에 좋다",
        }
    }

    masked = mask_body(body)
    user = masked["userRequest"]["user"]

    assert user["id"] == "***"
    assert user["type"] == "botUserKey"
    assert user["properties"]["isFriend"] is True
    for key in ("botUserKey", "plusfriendUserKey", "bot_user_key", "plusfriend_user_key", "app_user_id"):
        assert user["properties"][key] == "***"
    for raw in ("u-1", "k-1", "p-1", "a-1"):
        assert raw not in str(masked)


def test_서명이_붙은_사진_주소는_쿼리를_가린다():
    # 실채널 사진 요청의 모양이다. 서명 값은 가짜다.
    url = "https://talk.kakaocdn.net/dna/x/y/i_abc.jpg?credential=AAA&expires=1&signature=BBB"
    body = {"userRequest": {"utterance": url, "params": {"media": {"type": "image", "url": url}}}}

    masked = str(mask_body(body))

    assert "AAA" not in masked and "BBB" not in masked
    assert "https://talk.kakaocdn.net/dna/x/y/i_abc.jpg?(가림)" in masked


def test_일반_링크의_쿼리는_그대로_남긴다():
    url = "https://www.youtube.com/watch?v=0uEIFaWpd50"
    assert mask_body({"utterance": url}) == {"utterance": url}
