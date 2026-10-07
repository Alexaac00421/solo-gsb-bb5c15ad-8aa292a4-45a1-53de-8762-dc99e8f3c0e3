#!/usr/bin/env bash
# 端到端冒烟验证：需服务已在 $BASE_URL 运行（默认 http://localhost:8000）
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SUF="${RUN_ID:-$(date +%s%N | tail -c 7)}"   # 每次运行独立编号，避免与历史数据冲突
PASS=0; FAIL=0

req() { # method path body -> prints "code|body"
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -s -w '\n%{http_code}' -X "$method" "$BASE_URL$path" -H 'Content-Type: application/json' -d "$body"
  else
    curl -s -w '\n%{http_code}' -X "$method" "$BASE_URL$path"
  fi
}

check() { # description expected_code actual_response
  local desc="$1" want="$2" actual="$3"
  local code body
  code=$(echo "$actual" | tail -n1)
  body=$(echo "$actual" | sed '$d')
  if [ "$code" = "$want" ]; then
    echo "PASS  $desc (HTTP $code)"
    PASS=$((PASS+1))
  else
    echo "FAIL  $desc (want $want, got $code): $body"
    FAIL=$((FAIL+1))
  fi
  LAST_BODY="$body"
}

PAST="2000-01-01T00:00:00Z"
FUTURE="2099-01-01T00:00:00Z"
A_EXP="A-EXP-$SUF"        # 已到期、无封存 -> 可销毁
A_HOLD="A-HOLD-$SUF"      # 已到期、有封存 -> 不可销毁
A_FUT="A-FUT-$SUF"        # 未到期 -> 不可销毁
A_BOTH="A-BOTH-$SUF"      # 未到期且有封存 -> 两种原因同时出现
A_MISS="A-MISS-$SUF"      # 不存在 -> 整批报错
A_C1="A-C1-$SUF"          # 并发用：封存先提交
A_C2="A-C2-$SUF"          # 并发用：销毁先提交

echo "== 建档 =="
check "登记已到期档案" 201 "$(req POST /archives "{\"reg_no\":\"$A_EXP\",\"retain_until\":\"$PAST\"}")"
check "登记有封存档案" 201 "$(req POST /archives "{\"reg_no\":\"$A_HOLD\",\"retain_until\":\"$PAST\"}")"
check "登记未到期档案" 201 "$(req POST /archives "{\"reg_no\":\"$A_FUT\",\"retain_until\":\"$FUTURE\"}")"
check "登记未到期+封存档案" 201 "$(req POST /archives "{\"reg_no\":\"$A_BOTH\",\"retain_until\":\"$FUTURE\"}")"
check "重复登记冲突" 409 "$(req POST /archives "{\"reg_no\":\"$A_EXP\",\"retain_until\":\"$PAST\"}")"

echo "== 封存 =="
check "案件 CASE-1 封存" 201 "$(req POST "/archives/$A_HOLD/holds" '{"case_no":"CASE-1"}')"
check "案件 CASE-2 封存同档案" 201 "$(req POST "/archives/$A_HOLD/holds" '{"case_no":"CASE-2"}')"
check "同案件重复封存幂等" 200 "$(req POST "/archives/$A_HOLD/holds" '{"case_no":"CASE-1"}')"
check "封存不存在档案" 404 "$(req POST "/archives/$A_MISS/holds" '{"case_no":"CASE-1"}')"
check "封存 A_BOTH" 201 "$(req POST "/archives/$A_BOTH/holds" '{"case_no":"CASE-9"}')"

echo "== 销毁批次：多原因并存 =="
RQ1="RQ-1-$SUF"
check "批次裁决" 201 "$(req POST /destruction-batches "{\"request_no\":\"$RQ1\",\"reg_nos\":[\"$A_EXP\",\"$A_HOLD\",\"$A_FUT\",\"$A_BOTH\"]}")"
echo "$LAST_BODY" | grep -q '"destroyed":true'  && echo "  -> $A_EXP 已销毁" || { echo "FAIL: $A_EXP 应销毁"; FAIL=$((FAIL+1)); }
echo "$LAST_BODY" | grep -q 'NOT_EXPIRED'      && echo "  -> 未到期原因存在" || { echo "FAIL: 缺 NOT_EXPIRED"; FAIL=$((FAIL+1)); }
echo "$LAST_BODY" | grep -q 'ACTIVE_HOLD'      && echo "  -> 封存原因存在" || { echo "FAIL: 缺 ACTIVE_HOLD"; FAIL=$((FAIL+1)); }
# A_BOTH 应同时携带两种原因
python3 - "$LAST_BODY" "$A_BOTH" <<'EOF' || { echo "FAIL: A_BOTH 应同时含 NOT_EXPIRED 与 ACTIVE_HOLD"; }
import json,sys
r=json.loads(sys.argv[1])
item=[i for i in r["items"] if i["reg_no"]==sys.argv[2]][0]
codes={x["code"] for x in item["reasons"]}
assert codes=={"NOT_EXPIRED","ACTIVE_HOLD"}, codes
assert item["destroyed"] is False
print("  -> 未到期+封存双原因同时出现")
EOF

echo "== 幂等重试 =="
check "同请求号同内容重试返回原结果" 200 "$(req POST /destruction-batches "{\"request_no\":\"$RQ1\",\"reg_nos\":[\"$A_BOTH\",\"$A_FUT\",\"$A_HOLD\",\"$A_EXP\"]}")"
check "同请求号不同内容冲突" 409 "$(req POST /destruction-batches "{\"request_no\":\"$RQ1\",\"reg_nos\":[\"$A_EXP\"]}")"
check "凭据可复查" 200 "$(req GET "/destruction-batches/$RQ1")"

echo "== 整批失败：档案不存在 =="
RQ2="RQ-2-$SUF"
check "含不存在档案整批报错" 404 "$(req POST /destruction-batches "{\"request_no\":\"$RQ2\",\"reg_nos\":[\"$A_FUT\",\"$A_MISS\"]}")"
check "失败批次不留凭据" 404 "$(req GET "/destruction-batches/$RQ2")"
# A_FUT 不应被销毁
body=$(req GET "/archives/$A_FUT"); echo "$body" | sed '$d' | grep -q '"status":"ACTIVE"' \
  && echo "PASS  批内其他档案未被部分销毁" && PASS=$((PASS+1)) \
  || { echo "FAIL  A_FUT 被部分销毁"; FAIL=$((FAIL+1)); }

echo "== 解除封存：互不影响 =="
check "解除 CASE-1" 200 "$(req DELETE "/archives/$A_HOLD/holds/CASE-1")"
check "重复解除报 404" 404 "$(req DELETE "/archives/$A_HOLD/holds/CASE-1")"
body=$(req GET "/archives/$A_HOLD"); echo "$body" | sed '$d' | grep -q 'CASE-2' \
  && echo "PASS  CASE-2 封存不受解除影响" && PASS=$((PASS+1)) \
  || { echo "FAIL  CASE-2 封存被误解除"; FAIL=$((FAIL+1)); }

echo "== 并发：封存先提交则阻止销毁 =="
check "并发档案建档" 201 "$(req POST /archives "{\"reg_no\":\"$A_C1\",\"retain_until\":\"$PAST\"}")"
req POST "/archives/$A_C1/holds" '{"case_no":"CASE-RACE"}' > /dev/null
RQ3="RQ-3-$SUF"
check "封存后销毁被阻止" 201 "$(req POST /destruction-batches "{\"request_no\":\"$RQ3\",\"reg_nos\":[\"$A_C1\"]}")"
echo "$LAST_BODY" | grep -q '"destroyed":false' \
  && echo "PASS  生效封存阻止销毁" && PASS=$((PASS+1)) \
  || { echo "FAIL  封存未阻止销毁"; FAIL=$((FAIL+1)); }

echo "== 并发：销毁先提交则封存冲突 =="
check "并发档案2建档" 201 "$(req POST /archives "{\"reg_no\":\"$A_C2\",\"retain_until\":\"$PAST\"}")"
RQ4="RQ-4-$SUF"
check "销毁成功" 201 "$(req POST /destruction-batches "{\"request_no\":\"$RQ4\",\"reg_nos\":[\"$A_C2\"]}")"
check "销毁后封存报冲突" 409 "$(req POST "/archives/$A_C2/holds" '{"case_no":"CASE-LATE"}')"
check "销毁后解除报冲突" 409 "$(req DELETE "/archives/$A_C2/holds/CASE-LATE")"

echo "== 已销毁档案不可变更、历史可查 =="
check "重复销毁已销毁档案标记 ALREADY_DESTROYED" 201 "$(req POST /destruction-batches "{\"request_no\":\"RQ-5-$SUF\",\"reg_nos\":[\"$A_EXP\"]}")"
echo "$LAST_BODY" | grep -q 'ALREADY_DESTROYED' \
  && echo "PASS  ALREADY_DESTROYED 原因存在" && PASS=$((PASS+1)) \
  || { echo "FAIL  缺 ALREADY_DESTROYED"; FAIL=$((FAIL+1)); }
check "档案销毁历史可查" 200 "$(req GET "/archives/$A_EXP/destruction-items")"

echo
echo "通过 $PASS 项，失败 $FAIL 项"
[ "$FAIL" -eq 0 ]
