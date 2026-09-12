# 골든 테스트셋

이 프로젝트의 품질 기준은 "스펙을 구현했는가"가 아니라 **"골든셋을 통과하는가"**다.

## 파일 형식

`tests/golden/` 아래에 `*.yaml`로 둔다. 파일 하나에 케이스 여러 건을 담을 수 있다.
`_template.yaml`은 러너가 건너뛴다.

```yaml
- id: gold-001                              # 필수. 전체에서 유일해야 한다
  input: "냉동 블루베리가 당뇨를 완치시킨다"   # 필수. 어르신이 공유한 원문
  source_url: https://youtube.com/watch?v=…  # 선택
  expected: WARNING                          # 필수. 4개 라벨 중 하나
  expected_rule: R-021                       # 선택. 매칭될 규칙 ID까지 고정하고 싶을 때
  reason: 질병 완치 표현 + 해당 기능성 인정 없음  # 필수. 사람이 읽는 판정 이유
  판정입력:                                   # 아래 설명 참고
    in_scope: true
    transcript_quality: ok
    disease_cure_expression: true
    ad_violation_hit: false
    recognition: NOT_RECOGNIZED
    functionality_match: NOT_APPLICABLE
    supporting_papers: 0
    refuting_papers: 0
```

## 판정입력 절이 왜 따로 있는가

지금은 ③ 주장 구조화와 ④ 이중 검증이 아직 구현되지 않았다. 그래서 원문 한 줄에서
`recognition`이나 논문 수를 자동으로 얻을 수 없다. 그 값들을 `판정입력` 절에 손으로
적어 두고, 러너는 그것을 규칙 엔진에 넣어 `expected`와 비교한다.

③과 ④가 구현되면 러너는 `input`에서 `판정입력`을 직접 만들어 내고, 손으로 적은
`판정입력`은 **중간 산출물의 기대값**으로 역할이 바뀐다. 즉 지금 적어 두는 값은
나중에 버리는 값이 아니라 그때 그대로 쓰이는 자산이다.

`판정입력`이 없는 케이스는 러너가 건너뛴다(skip). 팀원이 원문과 기대 라벨만 먼저
채워 넣고 나중에 값을 보강하는 방식이 가능하다.

## 채우는 방법

1. `_template.yaml`을 복사해 `gold_<주제>.yaml`로 만든다
2. 원문과 기대 라벨, 그 이유를 적는다
3. `판정입력` 값을 적는다. 모르면 비워 두고 넘어간다
4. `pytest tests/golden` 실행
