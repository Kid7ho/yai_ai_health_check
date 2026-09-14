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
