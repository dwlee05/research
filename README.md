# 고뭉치 비서실

공저자들이 Dropbox에서 어떤 파일을 고쳤는지, 오늘·내일 일정이 어떤지를 한 번에 챙겨 주는
연구자용 비서입니다. [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview)
(`claude-agent-sdk`)로 만든 멀티 에이전트 프로그램입니다.

## 구조

```
사용자
 ├─ 터미널 · 아침 브리핑 · Slack @moongchi (고뭉치 봇)
 │   └─ 비서실장 고뭉치 ─ main 에이전트. 데이터에 직접 손대지 않고 Agent 도구로 일을 맡김
 │       ├─ 업뎃 (update) ─ 공저자 업데이트 담당
 │       │    └─ check_dropbox_updates  → Dropbox 폴더(20_연구-진행)의 공저자 변경 파일 목록 (내용은 안 읽음)
 │       └─ 일정 (schedule) ─ 캘린더 일정·날씨 담당
 │            ├─ get_schedule           → Mac 캘린더 앱(EventKit) 또는 ICS 캘린더 (Google·Outlook·iCloud)
 │            └─ get_weather            → 오늘·내일 날씨와 미세먼지 (Open-Meteo, 키 없음)
 ├─ Slack @update (업뎃 봇) · 터미널 --agent update     → 업뎃이 바로 답함 (Dropbox 도구만)
 └─ Slack @schedule (일정 봇) · 터미널 --agent schedule → '일정'이 바로 답함 (캘린더·날씨 도구만)
```

- **고뭉치**는 브리핑을 부탁받으면 업뎃과 '일정'에게 **동시에** 일을 맡기고, 두 보고를 합쳐
  ① 오늘의 일정 ② Dropbox 업데이트 두 부분으로 된 브리핑을 씁니다. `--brief`와 [아침 브리핑](#아침-브리핑-매일-자동으로-받기)은
  제목 바로 아래에 그날 서울 날씨 한 줄(Open-Meteo)을, 끝에 Chat KHU 남은 크레딧을 프로그램이 바로 붙입니다(LLM 호출 없음).
  고뭉치가 쓸 수 있는 도구는 Agent(하위 에이전트 호출) 하나뿐입니다.
- **업뎃**은 Dropbox 도구만 씁니다. 사용자 본인의 작업은 빼고 **공저자의 작업만** 보고합니다.
  `20_연구-진행` 폴더에서 공저자가 바꾼 **파일 목록만** 하위 폴더·사람별로 수정 시각과 폴더 링크를 붙여 알려 줍니다.
  토큰을 아끼려고 파일 내용은 읽지도 요약하지도 않으니, 내용은 직접 열어 확인하세요.
  - Overleaf 확인 기능은 제거했습니다(필요하면 git 기록에서 되살릴 수 있습니다). 예전 `.env`에 남은 `OVERLEAF_*`·`MY_*` 줄은 무시됩니다.
- **일정**('일정' 에이전트)은 캘린더 도구와 날씨 도구만 씁니다. 그날과 다음 날 일정, "지금 / 바로 다음 일정", 겹침과 빈 시간을 짧게 보고합니다.
  날씨를 묻거나 `내일 비 오면 일정 바꿔야 할까?`처럼 날씨 때문에 야외 일정이나 이동이 달라질지 물으면
  날씨 도구(`get_weather`)로 오늘·내일 날씨를 확인해 짧게 답합니다. 고뭉치도 이런 질문은 '일정'에게 맡깁니다.
- 업뎃과 일정은 고뭉치를 거치지 않고 **직접** 부를 수도 있습니다. Slack에서는 각자의 봇(`@update`, `@schedule`)을,
  터미널에서는 `--agent update` / `--agent schedule`을 씁니다. 이때도 자기 도구만 쓰고, 다른 도구나 Agent 도구는 쓸 수 없습니다.
- 데이터 도구는 모두 **읽기 전용**이고, 프로그램 안에서 도는 SDK MCP 서버(`mungchi`)로 묶여 있습니다.
- 모델은 `MUNGCHI_MODEL`(기본 `claude-opus-5-5`)이며, 업뎃·일정은 같은 모델을 이어받습니다(`inherit`).
- 터미널과 Slack은 같은 에이전트를 씁니다. Slack에서 부르는 방법은 아래 [Slack에서 부르기](#slack에서-부르기)를 보세요.

## 설치

**Python 3.10 이상**과 `git`이 필요합니다. 먼저 터미널에서 버전을 확인하세요.

```bash
python3 --version    # Python 3.10 이상이면 됩니다 (예: Python 3.14.2)
```

> macOS에는 `python` 명령이 없고 `python3`만 있는 경우가 많습니다. 그래서 가상환경(venv)은 `python3`로 만듭니다.
> **Mac에서 `python` 명령은 가상환경이 켜져 있을 때만 있습니다.**

```bash
git clone <이 저장소 주소> research
cd research
python3 -m venv .venv        # 가상환경 만들기 (처음 한 번만)
source .venv/bin/activate    # 가상환경 켜기
pip install -e .             # 설치 (개발/테스트까지: pip install -e '.[dev]')
cp .env.example .env         # 그다음 .env를 채웁니다 (아래 참고)
```

가상환경이 켜지면 프롬프트 맨 앞에 **`(.venv)`** 가 붙습니다(예: `(.venv) gildong@MacBook research %`).
이 표시가 있을 때만 `python`, `pip`, `mungchi` 명령이 이 저장소의 가상환경을 씁니다. 이 문서의 `python -m mungchi ...` 명령은
모두 가상환경을 켠 상태에서 실행합니다.

**터미널을 새로 열 때마다** 저장소 폴더로 가서 가상환경을 다시 켜야 합니다.

```bash
cd research                  # 저장소를 받은 폴더
source .venv/bin/activate    # 프롬프트 앞에 (.venv)가 붙으면 준비 끝
```

> macOS에서 python.org 설치 파일로 Python을 깔았다면 인증서가 없어 처음 Slack 연결이 `CERTIFICATE_VERIFY_FAILED`로
> 실패할 수 있습니다. 미리 `/Applications/Python 3.x/Install Certificates.command`를 한 번 실행해 두세요
> (자세한 내용은 [문제 해결](#문제-해결)).

## Claude 인증

고뭉치는 Claude API를 부르는 프로그램이라, 아래 **둘 중 하나**를 `.env`에 설정해야 합니다.

> 실행할 때마다 Claude API를 호출하므로 사용량에 따라 비용이 듭니다.
>
> Claude 구독(Pro·Max)이나 `claude` CLI 로그인으로는 쓰지 않습니다. Anthropic은 Agent SDK로 만든 프로그램에
> API 키 인증이나 지원되는 게이트웨이·클라우드 제공자를 쓰도록 안내합니다.

### 방법 1. Anthropic API 키 (공식 방법)

1. [Claude Console](https://platform.claude.com)에 로그인해 **API Keys**에서 키를 만듭니다(`sk-ant-`로 시작).
2. `.env`에 넣습니다.
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   MUNGCHI_MODEL=claude-opus-5-5
   ```
3. 요금은 쓴 만큼 Console 계정에 청구됩니다. Console 설정의 **Limits**에서 월 사용 한도(spend limit)를 정해 두면
   예상보다 많이 나오는 것을 막을 수 있습니다.

### 방법 2. Anthropic 호환 LLM 게이트웨이 (예: 대학에서 제공하는 게이트웨이)

학교나 기관이 Claude를 쓸 수 있는 LLM 게이트웨이를 제공하면, Anthropic API 키 대신 그 게이트웨이 키로 쓸 수 있습니다.

- 게이트웨이가 **Anthropic Messages 형식(`/v1/messages`)** 을 지원해야 합니다. OpenAI 호환 형식(`/v1/chat/completions`)만
  지원하는 게이트웨이는 그대로는 쓸 수 없습니다.
- 고뭉치가 읽은 데이터(Slack 메시지, 파일 이름, 캘린더 일정 제목 등)가 **게이트웨이 운영 기관을 거쳐** Claude로 갑니다.
  쓰기 전에 게이트웨이의 이용 정책(데이터 보관·활용 범위)을 확인하세요.

**예시: Chat KHU (운영: Mindlogic)**

```
ANTHROPIC_API_KEY=
ANTHROPIC_BASE_URL=https://factchat-cloud.mindlogic.ai/v1/gateway/claude
ANTHROPIC_AUTH_TOKEN=<게이트웨이에서 발급한 키>
MUNGCHI_MODEL=<게이트웨이에서 쓸 수 있는 모델 ID>
ANTHROPIC_DEFAULT_HAIKU_MODEL=<게이트웨이의 Haiku 모델 ID, 선택>
```

- `ANTHROPIC_API_KEY`는 **반드시 비워 두세요**. 값이 있으면 게이트웨이 인증이 실패합니다.
  셸에서 `export ANTHROPIC_API_KEY=...`를 해 두었다면 `unset ANTHROPIC_API_KEY`로 지우세요(셸의 값이 `.env`보다 우선합니다).
- 게이트웨이 화면에는 주소가 `https://factchat-cloud.mindlogic.ai/v1/gateway`로 나오지만, Claude용 주소는 끝에 `/claude`를 붙인
  `.../v1/gateway/claude`입니다.
- 게이트웨이의 모델 ID는 Anthropic과 다를 수 있습니다. 아래 [모델 확인](#모델-확인---list-models)으로 목록을 보고
  `MUNGCHI_MODEL`에 그대로 적으세요.
- `ANTHROPIC_DEFAULT_HAIKU_MODEL`은 Claude Code가 가벼운 보조 작업에 쓰는 Haiku 모델입니다. 게이트웨이에 Haiku가 있으면
  그 ID를 적습니다(선택).

### 모델 확인 (`--list-models`)

`.env` 설정 그대로 Claude API(또는 게이트웨이)에 쓸 수 있는 모델 목록을 물어봅니다. 에이전트를 실행하지 않고
`GET {ANTHROPIC_BASE_URL}/v1/models`(기본 `https://api.anthropic.com/v1/models`)만 호출하므로 모델 사용 요금이 들지 않습니다.

```bash
python -m mungchi --list-models
```

```
모델 목록 확인: GET https://factchat-cloud.mindlogic.ai/v1/gateway/claude/v1/models (인증: ANTHROPIC_AUTH_TOKEN)
HTTP 200: 모델 2개
  claude-opus-4-1
* claude-sonnet-4-5   ← 지금 MUNGCHI_MODEL
```

- 응답 상태와 모델 ID를 한 줄에 하나씩 보여 주고, 지금 `MUNGCHI_MODEL`과 같은 ID에 `*`를 붙입니다(위 ID는 예시입니다).
- 키 값은 출력하지 않고, 어느 변수(`ANTHROPIC_AUTH_TOKEN` 또는 `ANTHROPIC_API_KEY`)를 썼는지만 보여 줍니다.
- 실패하면(401, 404 등) 응답 일부와 무엇을 확인할지 한국어로 알려 줍니다. 게이트웨이가 모델 목록을 지원하지 않으면
  404가 나올 수 있으니, 그때는 게이트웨이 안내 문서에서 모델 ID를 확인하세요.

### 크레딧 확인 (Chat KHU)

Chat KHU(Mindlogic) 게이트웨이를 쓰면 남은 크레딧과 이번 달 사용량을 볼 수 있습니다. 게이트웨이의 크레딧 조회 주소
(`.../v1/gateway/credits/`, `.../v1/gateway/usage/`)만 부르고 에이전트(LLM)는 실행하지 않으므로 **크레딧이 들지 않습니다**.

```bash
python -m mungchi --credits
```

```
💳 Chat KHU 크레딧: 9,050.5 남음 / 10,000 (90.5%) · 11/01 갱신
이번 달 사용: 949.5 (10/01–10/07, 94회)
· claude-sonnet-5: 71회 · 536.7
· claude-opus-5-5: 23회 · 357.2
이 속도면 이번 달 약 4,200 사용 예상 (한도의 42%)
```

- 모델별 사용량은 많이 쓴 순서로 5개까지 보여 줍니다. 구매 크레딧이나 기관 지원 크레딧이 있으면 그 줄도 나옵니다.
- 예상 사용량은 지금까지의 속도로 이번 갱신 주기(갱신일 한 달 전부터 갱신일까지)를 다 쓴다고 보고 계산하며,
  주기가 시작되고 하루가 지나야 나옵니다.
- 인증은 `ANTHROPIC_AUTH_TOKEN`(없으면 `ANTHROPIC_API_KEY`)을 쓰고, 키는 출력하지 않습니다. 401·403이 나오면 `.env`의 키를 확인하세요.
- 주소는 `ANTHROPIC_BASE_URL`에서 끝의 `/claude`를 뺀 것입니다. 다른 주소를 쓰려면 `CREDITS_API_BASE`에 적습니다.
  Chat KHU가 아닌 게이트웨이나 Anthropic API 키로는 "크레딧 조회는 Chat KHU(Mindlogic) 게이트웨이에서만 됩니다."라고만 알려 줍니다.

**Slack에서 바로 묻기**: 세 봇 어디에서나 짧게 물으면 에이전트를 거치지 않고 바로 답합니다(LLM 호출 없음, 비용 없음).

- 예: `@고뭉치 크레딧`, `@업뎃 남은 크레딧 얼마나 남았어?`, DM으로 `잔액`, `사용량 보여줘`, `credits`
- **날씨**도 같습니다: `@고뭉치 날씨`, `@일정 오늘 서울 날씨 어때?`, DM으로 `날씨 알려줘`라고 보내면
  [브리핑의 날씨 줄](#날씨-open-meteo)과 같은 한 줄로 바로 답합니다(Open-Meteo, LLM 호출 없음).
  `날씨는?`, `오늘의 날씨`, `지금 날씨 어때?`, `서울 날씨 좀 알려줘`, `날씨 알려줄래?`, `날씨 알려주세요!`, `날씨 🙏`처럼
  앞에 오늘·오늘의·지금·현재, 곳(서울, `WEATHER_LABEL`, 여기), 뒤에 어때·알려줘·확인해줘·보여줘·궁금해 같은 말이 붙어도 되고,
  띄어쓰기와 끝의 문장부호·이모지는 상관없습니다.
- `크레딧 아끼려면 어떻게 해?`, `내일 비 오면 일정 바꿔야 할까?`, `날씨 좋은 날 야외 미팅 잡아줘`처럼 긴 질문은 지금처럼 에이전트에게 갑니다.
  날씨가 걸린 질문은 '일정'이 날씨 도구로 확인해 답합니다(에이전트가 답하므로 모델 호출 비용이 듭니다).
- 허용되지 않은 사람에게는 늘 그렇듯 거절만 하고, 이 답은 스레드의 대화 기록에 남기지 않습니다(같은 스레드에서 이어 묻는 대화에 영향 없음).

**잔액 알림**: Slack 봇(`python -m mungchi slack` 또는 [백그라운드 서비스](#백그라운드로-실행하기-추천))이 켜져 있는 동안
시작 1분 뒤와 그 뒤 1시간마다 크레딧을 확인합니다. 남은 크레딧이 `CREDIT_ALERT_PERCENT`(기본 10)% 아래로 내려가면
`SLACK_ALLOWED_USER_IDS`의 사람마다 고뭉치 봇(고뭉치 봇이 없으면 켜진 첫 봇)이 경고와 위 요약을 DM으로 보냅니다.

- 갱신 주기마다 **한 번만** 보냅니다(보낸 갱신일은 `.mungchi_state.json`에 기록). 다음 주기에 다시 내려가면 또 알립니다.
- `CREDIT_ALERT_PERCENT=0`이나 빈 값(`CREDIT_ALERT_PERCENT=`)이면 알림을 끕니다. `.env`를 고친 뒤에는 봇을 다시 시작하세요.
- 확인이 실패해도 로그에 한 줄 남기고(키는 지움) 봇은 그대로 돕니다. Chat KHU가 아닌 게이트웨이면 아무것도 하지 않습니다.

## 자격 증명 준비

소스는 필요한 것만 설정해도 됩니다. 설정하지 않은 소스는 고뭉치가 "설정 안 됨"과 함께
**빠진 환경변수 이름**을 알려 줍니다.

### 1. Dropbox

고뭉치는 Dropbox에서 **파일 목록(경로, 수정 시각, 마지막 수정자)만** 읽습니다.
토큰을 아끼려고 파일 내용은 내려받지 않습니다.

1. <https://www.dropbox.com/developers/apps> → **Create app** → **Scoped access** →
   **Full Dropbox**(공저자와 공유한 폴더를 보려면 필요) → 앱 이름을 정하고 만듭니다.
2. **Permissions** 탭에서 아래 세 가지를 체크하고 **Submit** 합니다.
   - `files.metadata.read`: 폴더의 파일 목록 읽기
   - `sharing.read`: 수정한 공저자의 이름 확인
   - `account_info.read`: 내 계정 확인(내가 고친 파일 빼기)
   > 권한을 바꾼 뒤에는 토큰을 새로 받아야 반영됩니다.
   > 예전 안내대로 `files.content.read`도 켜 두었다면 이제 필요 없으니 꺼도 됩니다.
3. 토큰 받기 (둘 중 하나)
   - **간단히 시험해 보기**: **Settings** 탭 → *Generated access token* → **Generate** →
     `DROPBOX_ACCESS_TOKEN`에 넣습니다. 이 토큰은 몇 시간 뒤 만료됩니다.
   - **매일 쓰기 (권장)**: 만료되지 않는 리프레시 토큰을 받습니다.
     1. **Settings** 탭의 *App key*, *App secret*을 `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`에 넣습니다.
     2. 브라우저에서 아래 주소를 열고(`<APP_KEY>` 바꾸기) 허용한 뒤 나오는 코드를 복사합니다.
        ```
        https://www.dropbox.com/oauth2/authorize?client_id=<APP_KEY>&response_type=code&token_access_type=offline
        ```
     3. 터미널에서 코드를 토큰으로 바꿉니다. 응답의 `refresh_token` 값을 `DROPBOX_REFRESH_TOKEN`에 넣습니다.
        ```bash
        curl https://api.dropboxapi.com/oauth2/token \
          -d code=<복사한_코드> -d grant_type=authorization_code \
          -u <APP_KEY>:<APP_SECRET>
        ```
4. 확인할 폴더는 기본으로 Dropbox 맨 위의 **`/20_연구-진행`** 입니다. 이 폴더가 다른 폴더 안에 있으면
   `DROPBOX_ROOT_FOLDER`에 전체 경로를 적습니다(예: `/Research/20_연구-진행`). 앞의 `/`는 빠져도 되고 끝의 `/`는 무시합니다.
   이 폴더 바로 아래 하위 폴더(논문별 폴더 등)를 단위로 묶어서 보고합니다.

### 2. 캘린더 (Mac 캘린더 앱 · Google 캘린더 · Outlook)

'일정' 에이전트가 일정을 읽는 방법은 두 가지입니다. `.env`의 `CALENDAR_SOURCE`(기본 `auto`)로 고릅니다.

- **방법 A. Mac 캘린더 앱에서 바로 읽기** (`CALENDAR_SOURCE=macos`): 봇을 Mac에서 돌린다면 이 방법을 권장합니다.
  캘린더를 공개하거나 ICS 주소를 만들 필요가 없습니다.
- **방법 B. ICS 주소로 읽기** (`CALENDAR_SOURCE=ics`): Mac이 아닌 컴퓨터이거나 Google·Outlook 주소를 쓰고 싶을 때.
- `auto`는 `CALENDAR_ICS_URLS`가 있으면 B, 비어 있으면 Mac에서는 A를 씁니다.

#### 방법 A. Mac 캘린더 앱에서 바로 읽기 (Mac 권장)

캘린더 앱에 보이는 캘린더(iCloud, Google, Exchange, "나의 Mac에" 모두)를 macOS의 EventKit으로 바로 읽습니다.

1. 저장소를 새로 받았다면(`git pull`) 가상환경을 켠 상태에서 **다시 설치**합니다.
   Mac에서만 필요한 `pyobjc`(EventKit)가 이때 함께 설치됩니다.
   ```bash
   cd research
   source .venv/bin/activate
   git pull
   pip install -e .
   ```
2. **macOS의 터미널 앱에서** 한 번 실행합니다(Claude API는 쓰지 않습니다).
   ```bash
   python -m mungchi --calendar-setup
   ```
   처음이면 macOS 확인 창이 뜹니다. **허용**을 누르세요. 창에는 고뭉치 대신 **터미널**(또는 그 명령을 실행한 앱) 이름이
   나올 수 있습니다. macOS는 권한을 Python을 실행한 앱에 주기 때문입니다.
   허용하면 계정별 캘린더 목록과, 오늘·내일 읽힐 일정 수와 처음 몇 개가 나옵니다. 맞는지 확인하세요.
   > 봇을 [백그라운드 서비스](#백그라운드로-실행하기-추천)로 돌릴 거라면 여기서 준 터미널 권한은 서비스에 쓰이지 않습니다.
   > 이 단계는 캘린더 목록과 `MACOS_CALENDARS` 필터를 확인하는 용도로 쓰고, 권한은 서비스를 설치한 뒤
   > "비서실 고뭉치" 확인 창에서 따로 허용하세요.
3. (선택) 일부 캘린더만 읽으려면 `.env`의 `MACOS_CALENDARS`에 캘린더 이름을 쉼표로 구분해 적습니다
   (예: `MACOS_CALENDARS=연구,수업`, 대소문자 무시). 비우면 모든 캘린더를 읽습니다. 적은 뒤 `--calendar-setup`을 다시 실행하면
   그 필터로 몇 개가 읽히는지, 찾지 못한 이름이 있는지 알려 줍니다.
4. `.env`의 `CALENDAR_ICS_URLS`는 **비워 두세요**(값이 있으면 `auto`는 ICS 주소를 읽습니다). 또는 `CALENDAR_SOURCE=macos`로 정합니다.
   예전에 이 용도로 iCloud 캘린더를 공개해 두었다면 이제 **공개 캘린더**를 꺼도 됩니다.
5. 켜 둔 봇이 있으면 **다시 시작**합니다. 백그라운드 서비스면 `python -m mungchi service restart`,
   터미널 탭에서 돌린다면 `Ctrl+C`로 멈춘 뒤 `python -m mungchi slack`.

- 권한을 거부했거나 "쓰기 전용"으로 정했다면 **시스템 설정 → 개인정보 보호 및 보안 → 캘린더**에서 봇을 실행하는 앱
  (백그라운드 서비스면 **비서실 고뭉치**, 터미널에서 띄웠으면 그 터미널 앱)을 **전체 접근**으로 바꾼 뒤 봇을 다시 시작하세요.
  고뭉치는 일정을 읽기만 하지만, macOS에서 일정을 읽으려면 '전체 접근'이 필요합니다.
- 터미널에서 띄운 봇은 권한을 직접 묻지 않습니다(Mac 앞에 아무도 없을 수 있으니까요). 아직 허용하지 않았으면 '일정' 에이전트가
  `--calendar-setup`을 실행하라고 알려 줍니다. 백그라운드 서비스는 시작할 때 아직 정하지 않은 상태면 **한 번** 묻고
  (최대 5분 기다림), 답이 없어도 봇은 그대로 켭니다.
- 캘린더 앱에 동기화된 내용을 읽으므로, 다른 기기에서 바꾼 일정은 Mac에 동기화된 뒤에 보입니다.

#### 방법 B. ICS 주소로 읽기

OAuth 없이 캘린더의 구독 주소(ICS, `webcal://…` 또는 `https://…`)만으로 읽습니다.
**캘린더가 여러 개면** 주소를 쉼표로 구분해 `CALENDAR_ICS_URLS`에 모두 넣습니다.

```
CALENDAR_ICS_URLS=webcal://p01-caldav.icloud.com/published/2/...,https://calendar.google.com/calendar/ical/.../basic.ics
```

##### 먼저: 캘린더가 어느 계정에 있는지 확인 (macOS 캘린더 앱)

캘린더 앱 왼쪽 사이드바를 보면 캘린더가 **iCloud**, **Google**, **Exchange**, **나의 Mac에** 같은 계정별로 묶여 있습니다.
쓰려는 캘린더가 어느 묶음 아래에 있는지에 따라 주소를 얻는 방법이 다릅니다.

##### iCloud 캘린더

1. 캘린더 앱 사이드바에서 캘린더를 Control-클릭(또는 오른쪽 클릭)하고 **캘린더 공유…** 를 고릅니다.
2. **공개 캘린더**를 켭니다.
3. 나오는 `webcal://…` 주소를 복사해 `CALENDAR_ICS_URLS`에 **그대로** 붙여 넣습니다.
   `webcal://`(또는 `webcals://`) 주소는 고뭉치가 알아서 `https://`로 바꿔 읽습니다.

> ⚠️ 공개 캘린더는 **주소를 아는 사람이면 누구나** 일정을 볼 수 있습니다. 이 주소를 다른 사람과 공유하거나
> 커밋하지 마세요. 유출됐다면 같은 화면에서 **공개 캘린더**를 꺼서 공유를 멈추세요.

##### Google 캘린더 (비공개 iCal 주소)

1. 컴퓨터에서 [Google 캘린더](https://calendar.google.com) → 오른쪽 위 톱니바퀴 → **설정**.
2. 왼쪽 **내 캘린더의 설정**에서 캘린더를 고릅니다.
3. **캘린더 통합** 항목의 **iCal 형식의 비공개 주소**(비공개 주소, iCal 형식)를 복사해 `CALENDAR_ICS_URLS`에 넣습니다.
   (회사·학교 계정은 관리자가 이 기능을 꺼 두었을 수 있습니다.)

> 비공개 주소는 **비밀번호와 같습니다**. 유출됐다면 같은 화면에서 재설정하세요.

##### Exchange·Outlook 캘린더

Outlook 웹의 설정 → 캘린더 → 공유 캘린더 → **캘린더 게시**에서 ICS 링크를 받습니다(기관에서 막아 두었을 수 있습니다).

##### "나의 Mac에" 캘린더

이 컴퓨터에만 저장된 로컬 캘린더라 구독 주소가 없어 **ICS로는 읽을 수 없습니다.** Mac에서는 방법 A로 읽을 수 있습니다.

> 고뭉치는 캘린더 주소를 출력이나 오류 메시지, 로그에 내보내지 않습니다(`webcal://` 형식과 바꾼 `https://` 형식 모두).

## 사용법

```bash
# 대화 모드: 여러 번 주고받기. exit 또는 종료 를 입력하면 끝납니다.
python -m mungchi

# 오늘 브리핑 한 번 (제목 아래 🌤️ 날씨, ① 오늘의 일정 ② Dropbox 업데이트, 끝에 💳 Chat KHU 크레딧)
python -m mungchi --brief

# 질문 한 번
python -m mungchi "지난 48시간 동안 공저자들이 Dropbox에서 무슨 파일을 고쳤어?"
python -m mungchi "내일 오후에 비는 시간 있어?"

# 고뭉치를 거치지 않고 업뎃이나 '일정'에게 바로 묻기 (질문 없이 쓰면 대화 모드)
python -m mungchi --agent update "지난 48시간 동안 누가 무슨 파일 고쳤어?"
python -m mungchi --agent schedule

# Slack 봇 실행 / 오늘 브리핑을 지금 바로 Slack에 올리기 (아래 "Slack에서 부르기", "아침 브리핑" 참고)
python -m mungchi slack
python -m mungchi --brief --slack

# 쓸 수 있는 모델 ID 확인 (에이전트 실행 없음, 위 "모델 확인" 참고)
python -m mungchi --list-models

# Chat KHU 남은 크레딧과 이번 달 사용량 (LLM 호출 없음, 위 "크레딧 확인" 참고)
python -m mungchi --credits

# 오늘 서울 날씨 한 줄 (Open-Meteo, LLM 호출 없음, 아래 "아침 브리핑"의 "날씨" 참고)
python -m mungchi --weather

# Mac 캘린더 앱 연결 (처음 한 번, macOS 터미널에서. 위 "캘린더"의 방법 A 참고)
python -m mungchi --calendar-setup

# 업뎃이 Dropbox 변경을 못 찾을 때 원인 확인 (Claude API 안 씀, 아래 "업뎃이 변경을 못 찾을 때" 참고)
python -m mungchi --dropbox-check --hours 72

# Slack 봇을 macOS 백그라운드 서비스로 (아래 "백그라운드로 실행하기" 참고)
python -m mungchi service install
python -m mungchi service status

# 도움말
python -m mungchi --help
python -m mungchi service --help
```

`pip install -e .`를 했다면 `python -m mungchi` 대신 `mungchi`로 실행해도 됩니다.
질문 자리에 `slack` 한 단어만 쓰거나 맨 앞에 `service`를 쓰면 질문이 아니라 명령으로 처리합니다
(`python -m mungchi "service 상태 알려줘"`처럼 따옴표로 묶은 문장은 그대로 질문입니다).
고뭉치의 답은 표준 출력(stdout)으로 흘러나오고, `→ 업뎃에게 맡기는 중...` 같은 진행 표시는
표준 오류(stderr)로 나옵니다. 그래서 `python -m mungchi --brief > 오늘.md`처럼 브리핑만 파일로 저장할 수 있습니다.
`--brief`는 Slack으로 받는 브리핑과 같은 모양(제목, 날씨, 일정, Dropbox, 크레딧)이라 다 만들어진 뒤 한 번에 나옵니다.

### 확인 범위

- 기간을 말하지 않고 업뎃이나 고뭉치에게 물으면(Slack, `--agent update`, 질문 한 번, 대화 모드) **최근 24시간**의 변경을 봅니다
  (폴더 아래 하위 폴더까지 모두). 몇 번을 물어도 아무것도 바뀌지 않아서, 물어본 순서에 따라 결과가 달라지지 않습니다.
- **브리핑**(아침 브리핑, `--brief`, `--brief --slack`)만 **지난 브리핑 이후**의 변경을 봅니다. 이 브리핑 기준 시각은
  `.mungchi_state.json`에 저장되고(`MUNGCHI_STATE_FILE`로 경로 변경), 브리핑이 Dropbox를 제대로 확인했을 때만 지금으로 바뀝니다.
  기록이 없으면(첫 브리핑) 최근 `LOOKBACK_DAYS`일(기본 1일 = 최근 24시간)을 봅니다. Slack에서 `@고뭉치`만 불러 받는 브리핑은 질문으로 치므로
  최근 24시간을 보고 기준 시각을 바꾸지 않습니다.
- "최근 3일", "오늘", "이번 주", "지난 48시간"처럼 기간을 말하면 언제나 그 범위로 보고, 브리핑 기준 시각은 바꾸지 않습니다.
- Office의 `~$…` 파일, LibreOffice의 `.~lock.…` 파일, `.DS_Store`, `Thumbs.db`, `desktop.ini`, `*.tmp`·`*.temp`, 편집기의
  `*.swp`·`*.swo`, Stata의 `*.stswp` 같은 임시·잠금 파일은 처음부터 뺍니다.
- 변경이 없으면 왜 없는지 한 줄로 알려 줍니다(예: "최근 24시간 동안 공저자가 바꾼 파일이 없어요").
  그래도 이상하면 [업뎃이 변경을 못 찾을 때](#업뎃이-변경을-못-찾을-때)를 보세요.
- 하위 폴더마다 Dropbox 폴더 링크를 하나씩 붙입니다. Slack에서는 `• *01_Youn* 📂 열기`처럼 폴더 이름 옆의 링크로,
  터미널에서는 폴더 이름 아래 줄에 주소만 나옵니다.
- Dropbox는 파일 목록만 봅니다. 한 번에 최근 60개 파일까지 이름과 수정 시각을 적고,
  그보다 많으면 나머지는 하위 폴더·사람별 개수만 알려 줍니다.

## 아침 브리핑 (매일 자동으로 받기)

Slack 봇이 켜져 있으면 고뭉치가 매일 아침 정해 둔 시각에 브리핑을 Slack으로 보내 줍니다.
[백그라운드 서비스](#백그라운드로-실행하기-추천)로 돌리면 cron 없이 매일 받을 수 있고, 캘린더 권한도 서비스 앱의 권한을 그대로 씁니다
(Slack 설정은 아래 [Slack에서 부르기](#slack에서-부르기) 참고).

브리핑은 이 순서로 옵니다.

1. **🌤️ 날씨**(제목 바로 아래 한 줄): 그날 서울 날씨를 프로그램이 바로 붙입니다(LLM 호출 없음, 아래 [날씨](#날씨-open-meteo) 참고).
2. **오늘의 일정**: '일정' 에이전트가 **오늘 하루치** 일정을 확인합니다. 지금 진행 중이거나 곧 시작하는 일정이 있으면 맨 앞에 둡니다.
3. **Dropbox 업데이트**: 업뎃이 **지난 브리핑 이후**(기록이 없으면 최근 `LOOKBACK_DAYS`일, 기본 1일) 공저자가 바꾼 파일 목록을
   알려 줍니다. 파일 내용은 읽지 않습니다.
4. **💳 Chat KHU 크레딧**: 남은 크레딧과 이번 달 사용량을 프로그램이 바로 붙입니다(LLM 호출 없음, `--credits`와 같은 내용).
   Chat KHU 게이트웨이를 쓰지 않거나 확인하지 못하면 한 줄 안내만 붙습니다.

```
☀️ 오늘의 브리핑 (10/08 목)
🌤️ 서울 날씨: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통

① 오늘의 일정
• 지금 / 바로 다음 일정: 10:00 랩 미팅 (2시간 뒤)
• 10:00–11:00 랩 미팅 (302호)

② Dropbox 업데이트
• 01_Youn 📂 열기
  • 김공저: draft.tex (10/07 22:14)
내용은 직접 확인해 주세요.

💳 Chat KHU 크레딧: 9,050.5 남음 / 10,000 (90.5%) · 11/01 갱신
이번 달 사용: 949.5 (10/01–10/08, 94회)
```

### 켜기

1. `.env`에 보낼 시각을 넣습니다(24시간제 `HH:MM`, `TIMEZONE` 기준).
   ```bash
   BRIEF_TIME=07:00
   # (선택) 평일(월–금)에만 받기. 기본은 daily(매일)
   BRIEF_DAYS=weekdays
   # (선택) 채널로 받기. 비워 두면 SLACK_ALLOWED_USER_IDS의 사람마다 고뭉치 봇 DM으로 받습니다
   SLACK_BRIEF_CHANNEL=C0123ABCD
   ```
2. 서비스를 다시 시작합니다(터미널에서 `python -m mungchi slack`으로 돌린다면 Ctrl+C로 끄고 다시 실행).
   ```bash
   python -m mungchi service restart
   ```
3. 잘 들어갔는지 확인합니다.
   ```bash
   python -m mungchi service status
   ```
   `- 아침 브리핑: 매일 07:00 (Asia/Seoul) → DM (허용된 사용자 1명)`과
   `- 마지막 아침 브리핑 (last_brief_date): ...`가 보이면 됩니다. 봇이 시작할 때 로그(`python -m mungchi service logs`)에도
   같은 내용이 한 줄 남습니다.

- **보내는 곳**: `SLACK_BRIEF_CHANNEL`이 있으면 그 채널로(멤버 ID를 넣으면 그 사람과의 DM으로), 없으면
  `SLACK_ALLOWED_USER_IDS`의 사람마다 고뭉치 봇 DM으로 보냅니다. 에이전트는 한 번만 돌고 같은 브리핑이 모두에게 갑니다.
- 고뭉치 봇(`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`)이 꺼져 있으면 보내지 않고 로그에 경고를 남깁니다.
- `BRIEF_TIME`을 비우면 아침 브리핑이 꺼집니다. `7시`처럼 잘못 적으면 봇은 그대로 돌고 아침 브리핑만 꺼진 채 로그에 경고가 남습니다.
- 브리핑 스레드에서 고뭉치에게 답하면(채널이면 `@고뭉치` 멘션, DM이면 그 스레드에 답장) 브리핑에 이어서 물을 수 있습니다.
- 브리핑을 만들지 못해도 제목, 날씨, 짧은 실패 안내, 크레딧은 보냅니다. 실패한 날은 다시 보내지 않고 로그에 "아침 브리핑 실패" 줄을 남깁니다.

### 날씨 (Open-Meteo)

제목 바로 아래 날씨 줄은 프로그램이 [Open-Meteo](https://open-meteo.com)(무료, 키 필요 없음)에서 받아 붙입니다.
에이전트가 일하는 동안 따로 받아 오고, 모델에게는 보내지 않습니다(`--brief`, `--brief --slack`, 아침 브리핑 모두 같음).

- 그날의 날씨, 최저·최고 기온(반올림), 강수확률을 보여 주고, 강수확률이 60% 이상이면 끝에 `☔ 우산 챙기세요`가 붙습니다.
- **미세먼지**는 지금의 PM10·PM2.5를 한국 기준(PM10 30/80/150, PM2.5 15/35/75 µg/m³ 이하면 좋음/보통/나쁨, 넘으면 매우나쁨)으로
  나누고 둘 중 나쁜 쪽을 적습니다. 미세먼지를 받지 못하면 그 부분만 빠집니다.
- 날씨를 받지 못하면 `🌤️ 서울 날씨: 가져오지 못했어요` 한 줄만 붙고 브리핑은 그대로 갑니다.
- 브리핑에서 빼려면 `.env`에 `BRIEF_WEATHER=off`(`0`, `false`도 됨)를 넣습니다. 기본은 켜짐입니다.
- 다른 곳의 날씨를 보려면 `WEATHER_LABEL`(보여 줄 이름, 기본 `서울`)과 `WEATHER_LAT`·`WEATHER_LON`(위도·경도, 기본 37.5665·126.9780)을
  적습니다. 위도·경도를 잘못 적거나 하나만 적으면 로그에 경고를 남기고 서울 날씨를 보여 줍니다. 날짜와 시간대는 `TIMEZONE`을 따릅니다.
- 터미널에서 날씨 한 줄만 보려면 `python -m mungchi --weather`. Slack에서는 세 봇 어디에서나 `날씨`, `오늘 날씨 어때?`처럼
  짧게 물으면 바로 답합니다([Slack에서 바로 묻기](#크레딧-확인-chat-khu) 참고).
- 내일 날씨나 날씨에 따라 일정을 바꿀지 같은 질문은 '일정' 에이전트가 날씨 도구(`get_weather`)로 답합니다.
  이 도구는 같은 Open-Meteo에서 오늘·내일 날씨를 받아 오며, 모레 이후 날씨는 알려 주지 않습니다.
- 터미널에서는 되는데 Slack에서 날씨가 안 되면, 코드를 받은 뒤 봇을 다시 켰는지 확인하세요
  (백그라운드 서비스면 `python -m mungchi service restart`).

### 지금 바로 시험하기

```bash
python -m mungchi --brief --slack
```

아침 브리핑과 같은 곳으로 같은 모양의 브리핑을 지금 바로 보냅니다(따로 "지금 보내기" 명령은 없습니다).
시험으로 보내도 그날 아침 브리핑은 예정대로 갑니다. 다만 시험 브리핑도 브리핑이라 Dropbox 기준 시각을 옮기므로,
다음 브리핑에는 그 뒤의 변경만 나옵니다. Slack 없이 터미널에서 보려면 `python -m mungchi --brief`.

### Mac이 잠자고 있었다면

- 봇은 30초마다 지금 시각(벽시계)을 보고, `BRIEF_TIME`이 지났는데 오늘 브리핑을 아직 보내지 않았으면 바로 보냅니다.
  그래서 7시에 Mac이 잠자고 있다가 8시 10분에 깨어나도(또는 봇을 늦게 켜도) 그때 보냅니다.
- **정오(12:00)가 지나면** 그날은 건너뛰고 로그에 한 줄 남깁니다. 이 시각은 `BRIEF_CATCHUP_UNTIL`로 바꿀 수 있고,
  `BRIEF_TIME`이 이 시각보다 늦으면 그날 자정까지 보냅니다.
- 하루에 한 번만 보냅니다. 보내기 시작할 때 날짜를 `.mungchi_state.json`의 `last_brief_date`에 먼저 적어 두므로,
  도중에 봇이 죽었다 다시 켜져도 같은 날 두 번 보내지 않습니다.
- 정시에 받으려면 그 시각에 Mac이 깨어 있어야 합니다. **시스템 설정 → 에너지**에서
  **"디스플레이가 꺼져 있을 때 자동으로 잠자기 방지"** 를 켜 두세요(MacBook은 **시스템 설정 → 배터리 → 옵션**의
  전원 어댑터 항목이며, 전원에 연결되어 있어야 합니다).
- (선택) Mac이 매일 아침 스스로 깨어나게 할 수도 있습니다. **관리자 암호가 필요하고 Mac 전체의 전원 설정을 바꾸니** 필요할 때만 쓰세요.
  ```bash
  sudo pmset repeat wakeorpoweron MTWRFSU 06:55:00   # 매일 06:55에 깨우기(꺼져 있으면 켜기)
  pmset -g sched                                     # 지금 걸린 일정 확인
  sudo pmset repeat cancel                           # 지우기
  ```
  `BRIEF_TIME`보다 몇 분 앞으로 잡으세요. `pmset repeat`는 반복 일정을 하나만 두므로, 이미 다른 반복 일정이 있으면 이것으로 바뀝니다.
  깨어난 뒤 금방 다시 잠들 수 있으니 위의 잠자기 방지 설정과 함께 쓰세요.

### 예전에 cron으로 받던 경우

예전 안내대로 `crontab`에 `--brief --slack` 줄을 넣어 두었다면 `crontab -e`로 그 줄을 지우세요. 그대로 두면 브리핑을 두 번 받습니다.
cron 작업은 Mac 캘린더 앱 권한을 받지 못할 수 있어서(터미널도 서비스 앱도 아니기 때문), 이제는 서비스 안에서 보내는 아침 브리핑을 권합니다.
Linux에서도 [systemd 서비스](#linux에서는)로 `python -m mungchi slack`을 돌리고 `BRIEF_TIME`을 넣으면 같은 방식으로 받습니다.

## Slack에서 부르기

내 Slack 워크스페이스에 봇 **세 개**를 따로 들일 수 있습니다. 봇마다 Slack 앱을 하나씩 만들고,
쓰고 싶은 봇만 만들어도 됩니다.

- **고뭉치** (`@moongchi`, 앱 이름 "비서실 고뭉치"): 비서실장. 업뎃과 '일정'에게 일을 맡겨 브리핑하고,
  무엇을 물어도 알맞은 팀원에게 맡깁니다.
  - 예: `@고뭉치`(내용 없이 멘션만 하면 오늘 브리핑), `@고뭉치 내일 오후에 논문A 검토할 시간 있어?`
- **업뎃** (`@update`, 앱 이름 "업뎃"): 공저자가 Dropbox에서 바꾼 파일 목록을 바로 알려 줍니다.
  - 예: `@업뎃`(멘션만 하면 "공저자 업데이트 확인해줘"), `@업뎃 지난 48시간 동안 누가 무슨 파일 고쳤어?`
- **일정** (`@schedule`, 앱 이름 "일정"): 캘린더 일정과 오늘·내일 날씨를 바로 알려 줍니다.
  - 예: `@일정`(멘션만 하면 "오늘과 내일 일정 알려줘"), `@일정 금요일 오후에 비는 시간 있어?`, `@일정 내일 비 오면 야외 미팅 미뤄야 할까?`

세 봇은 모두 **한 프로세스**(`python -m mungchi slack`)에서 함께 돌아가고, 토큰을 넣은 봇만 켜집니다.
봇은 내 컴퓨터에서 **Socket Mode**로 돌기 때문에 공개 URL이나 서버가 필요 없습니다.
`BRIEF_TIME`을 넣으면 고뭉치 봇이 매일 아침 브리핑도 보냅니다([아침 브리핑](#아침-브리핑-매일-자동으로-받기)).
**브리핑 올리기**(`python -m mungchi --brief --slack`)는 같은 브리핑을 지금 바로 같은 곳(채널, 없으면 DM)에 올립니다(고뭉치 봇 토큰 사용).

> **비용 주의**: 어느 봇이든 멘션이나 DM 한 번마다 Claude API를 호출합니다. 고뭉치는 업뎃·일정까지 모델을 부르고,
> 업뎃·일정 봇은 자기 에이전트 하나만 부릅니다. 동시에 처리하는 요청 수는 세 봇을 합쳐
> `SLACK_MAX_CONCURRENT`(기본 2)로 제한합니다. 단, `크레딧`이나 `날씨`처럼 크레딧이나 오늘 날씨만 묻는 짧은 말은
> 모델을 부르지 않습니다([크레딧 확인](#크레딧-확인-chat-khu)의 "Slack에서 바로 묻기" 참고).

### 1. 매니페스트로 앱 만들기 (앱마다 반복)

[`slack_manifests/`](slack_manifests) 폴더에 봇마다 매니페스트가 있습니다.

- [`moongchi.yaml`](slack_manifests/moongchi.yaml): 고뭉치 (앱 이름 "비서실 고뭉치", 핸들 `@moongchi`)
- [`update.yaml`](slack_manifests/update.yaml): 업뎃 (앱 이름 "업뎃", 핸들 `@update`)
- [`schedule.yaml`](slack_manifests/schedule.yaml): 일정 (앱 이름 "일정", 핸들 `@schedule`)

쓰려는 봇마다 아래를 반복합니다.

1. <https://api.slack.com/apps> → **Create New App** → **From an app manifest** → 워크스페이스를 고릅니다.
2. 매니페스트 내용을 **YAML** 탭에 붙여 넣고 **Next** → **Create**.
   세 앱의 권한(bot scope)은 똑같이 꼭 필요한 다섯 개뿐입니다.
   - `app_mentions:read`: 채널에서 멘션 받기
   - `chat:write`: 답 올리기와 고치기
   - `im:history`, `im:read`, `im:write`: 봇과의 DM 읽고 쓰기

   이벤트는 `app_mention`, `message.im` 두 가지이고, App Home의 **Messages 탭**(DM 보내기)이 켜져 있습니다.

   > **Slack에서 보이는 이름과 멘션**
   > - 앱은 Slack에서 "비서실 고뭉치", "업뎃", "일정"으로 보입니다.
   > - 멘션할 때는 `@고뭉치`, `@업뎃`, `@일정`이라고 입력하고 자동완성에서 봇을 고르면 됩니다.
   >   실제 핸들은 `@moongchi`, `@update`, `@schedule`입니다(Slack은 봇 핸들에 ASCII만 허용합니다).
   > - (선택) 앱마다 App Home → **Your App's Presence in Slack** → **Edit**에서 Display Name을 한글 이름으로 바꿔 보세요.
   >   Slack이 받아 주면 멘션도 한글로 보입니다.
   > - 예전 `slack_manifest.yaml`로 고뭉치 앱을 이미 만들었다면 다시 만들 필요가 없습니다.
   >   그 파일은 `slack_manifests/moongchi.yaml`로 옮겼고 권한과 이벤트는 같습니다.

### 2. Socket Mode 켜고 앱 토큰(xapp-) 만들기 (앱마다 반복)

1. 앱 설정 왼쪽의 **Socket Mode**에서 **Enable Socket Mode**가 켜져 있는지 확인합니다(매니페스트로 켜집니다).
2. **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes** → 이름(예: `socket`)을 적고
   **Add Scope**에서 `connections:write`를 고른 뒤 **Generate**.
3. `xapp-`로 시작하는 토큰을 그 앱의 App 토큰 변수에 넣습니다(아래 4번 참고).

### 3. 워크스페이스에 설치하고 봇 토큰(xoxb-) 받기 (앱마다 반복)

1. **OAuth & Permissions**(또는 **Install App**) → **Install to Workspace** → **허용**.
2. **Bot User OAuth Token**(`xoxb-`로 시작)을 복사해 그 앱의 Bot 토큰 변수에 넣습니다(아래 4번 참고).
   > 권한을 바꾼 뒤에는 **Reinstall to Workspace**를 해야 반영됩니다.

### 4. 앱별 토큰을 `.env`에 넣기

| 봇 (매니페스트) | Bot User OAuth Token (`xoxb-`) | App-Level Token (`xapp-`) |
| --- | --- | --- |
| 고뭉치 (`moongchi.yaml`) | `SLACK_BOT_TOKEN` | `SLACK_APP_TOKEN` |
| 업뎃 (`update.yaml`) | `SLACK_UPDATE_BOT_TOKEN` | `SLACK_UPDATE_APP_TOKEN` |
| 일정 (`schedule.yaml`) | `SLACK_SCHEDULE_BOT_TOKEN` | `SLACK_SCHEDULE_APP_TOKEN` |

- 봇은 각각 선택입니다. 두 토큰을 모두 넣은 봇만 켜집니다. 하나 이상은 있어야 합니다.
- 한 봇에 토큰을 하나만 넣으면 봇이 시작하지 않고 빠진 변수 이름을 알려 줍니다.
- 봇마다 따로 만든 앱의 토큰을 넣으세요. 같은 토큰을 두 변수에 넣으면 시작하지 않습니다.

### 5. 내 멤버 ID 넣기 (`SLACK_ALLOWED_USER_IDS`)

세 봇 모두 Dropbox·캘린더의 개인 정보를 읽으므로 **`SLACK_ALLOWED_USER_IDS`에 적힌 사람에게만** 답합니다.
이 값은 세 봇이 함께 쓰고, 비어 있으면 봇이 아예 시작하지 않습니다. 다른 사람이 봇을 부르면 스레드에
"이 봇은 소유자만 사용할 수 있어요"라고 한 번만 답하고, 에이전트는 실행하지 않습니다.

1. Slack에서 내 프로필 사진 → **프로필** → **⋮** → **멤버 ID 복사**.
2. `U`로 시작하는 값을 `SLACK_ALLOWED_USER_IDS`에 넣습니다. 여러 명이면 쉼표로 구분합니다.

### 6. 채널 ID 찾고 봇 초대하기

브리핑은 `SLACK_BRIEF_CHANNEL`을 비워 두면 `SLACK_ALLOWED_USER_IDS`의 사람에게 고뭉치 봇 DM으로 갑니다.
DM으로 받을 거라면 1–2는 건너뛰어도 됩니다.

1. 브리핑을 받을 채널을 엽니다. 나만 보는 **비공개 채널**을 권장합니다.
2. 채널 이름을 누르면 나오는 창의 맨 아래 **채널 ID**(`C`로 시작)를 복사해 `SLACK_BRIEF_CHANNEL`에 넣습니다.
3. 그 채널에서 쓸 봇을 각각 초대합니다. 초대하지 않은 봇을 멘션하면 답하지 못합니다(`not_in_channel`).
   ```
   /invite @moongchi
   /invite @update
   /invite @schedule
   ```
   다른 채널에서 멘션으로 부를 때도 그 봇이 그 채널에 초대되어 있어야 합니다.

채널 대신 내 멤버 ID(`U…`)를 `SLACK_BRIEF_CHANNEL`에 넣으면 고뭉치 봇과의 DM(앱의 메시지 탭)으로 받습니다.

### 7. 봇 실행하기

```bash
python -m mungchi slack
```

토큰을 넣은 봇이 모두 한 프로세스에서 켜지고, 표준 오류에 켜진 봇이 나옵니다.

```
Slack 봇 3개를 시작했습니다 (Socket Mode, 허용된 사용자 1명): 고뭉치(@moongchi), 업뎃(@update), 일정(@schedule). 멈추려면 Ctrl+C를 누르세요.
```

- **고뭉치**: `@고뭉치 어제 공저자들이 뭐 고쳤어?`라고 쓰면 스레드에 "🗂️ 고뭉치가 확인 중이에요..."가 먼저 뜨고,
  `→ 업뎃에게 맡기는 중...` 같은 진행 상황으로 바뀌다가 답으로 바뀝니다. 답이 길면 스레드에 나눠 올립니다.
- **업뎃 / 일정**: `@업뎃 누가 무슨 파일 고쳤어?`, `@일정 내일 일정 알려줘`처럼 부르면 "📝 업뎃이 확인 중이에요...",
  "⏰ 일정이 확인 중이에요..."가 떴다가 답으로 바뀝니다. 고뭉치를 거치지 않으니 진행 상황 표시 없이 바로 답합니다.
- **내용 없이 멘션만** 하면 고뭉치는 오늘 브리핑, 업뎃은 "공저자 업데이트 확인해줘", 일정은 "오늘과 내일 일정 알려줘"로 알아듣습니다.
- **이어서 묻기**: 같은 스레드에서 같은 봇을 다시 멘션하면 그 봇과의 앞 대화를 이어 갑니다. 대화는 봇마다 따로라서,
  업뎃과 이야기하던 스레드에서 고뭉치를 부르면 고뭉치는 새 대화로 시작합니다. 채널에서는 멘션한 메시지만 봇에게
  전달되므로 스레드 안에서도 멘션을 붙여야 합니다.
- **DM**: 각 앱의 **메시지** 탭에서 그냥 보내면 됩니다. 답은 보낸 메시지의 스레드로 오고, 그 스레드에 답장하면 이어 갑니다.
- 스레드와 대화의 연결은 상태 파일과 같은 폴더의 `.mungchi_slack_threads.json`에 봇별로(세 봇 합쳐 최근 200개까지)
  저장되어, 봇을 다시 켜도 이어집니다. 예전 버전이 저장한 연결은 고뭉치 것으로 이어집니다. 대화 기록은 Claude Code가
  `~/.claude/projects/` 아래에 폴더별로 저장하므로 **봇과 `--brief --slack`은 항상 같은 폴더(저장소)에서 실행**하세요.
- 공개 채널에서 부르면 답(공저자 작업, 일정)도 그 채널 사람들이 봅니다. DM이나 비공개 채널을 쓰세요.

**계속 켜 두기**: 터미널 탭을 열어 두지 않아도 되도록, Mac에서는 아래
[백그라운드로 실행하기 (추천)](#백그라운드로-실행하기-추천)의 `python -m mungchi service install`을 쓰세요.
로그인하면 자동으로 켜지고, 봇이 죽으면 다시 켜지고, 캘린더 권한도 그 서비스 앱이 받습니다.
(tmux·nohup이나 launchd에 `python -m mungchi slack`을 직접 등록하는 예전 방법은 더 이상 권하지 않습니다.
특히 launchd에 Python을 직접 등록하면 Mac 캘린더 앱을 읽을 권한을 받을 앱이 없습니다.)

### 8. 브리핑을 Slack으로 받기

```bash
python -m mungchi --brief --slack
```

- `SLACK_BRIEF_CHANNEL`이 있으면 그 채널로, 없으면 `SLACK_ALLOWED_USER_IDS`의 사람마다 고뭉치 봇 DM으로 올립니다
  (매일 아침 자동으로 오는 [아침 브리핑](#아침-브리핑-매일-자동으로-받기)과 같은 곳, 같은 모양).
- 첫 메시지에 굵은 제목 "☀️ 오늘의 브리핑 (10/05 월)"(날짜는 `TIMEZONE` 기준), 그 아래 날씨 한 줄과 브리핑 본문이 함께 올라가서
  바로 읽을 수 있습니다. 본문이 길면(약 3,500자 초과) 나머지는 그 메시지의 스레드에 이어 붙습니다. 맨 끝은 Chat KHU 크레딧입니다.
- 봇이 켜져 있으면 그 스레드에서 `@고뭉치 첫 번째 항목 자세히 알려줘`처럼 고뭉치를 멘션해(DM이면 스레드에 답장해) 브리핑에 이어서 물을 수 있습니다.
- 브리핑을 만들지 못하면 제목, 날씨, 실패 안내, 크레딧을 올리고 0이 아닌 종료 코드로 끝납니다(자세한 내용은 표준 오류에 남습니다).
- `--slack` 없이 `--brief`만 쓰면 같은 브리핑이 터미널(표준 출력)로 나옵니다.

### Slack 문제 해결

설치·인증·인증서 문제는 아래 [문제 해결](#문제-해결)을 보세요.

- `[오류] Slack 봇을 시작할 수 없습니다.`: 빠진 환경변수 이름이 함께 나옵니다. `.env`를 채우세요.
  토큰을 하나만 넣은 봇이 있으면 그 봇의 빠진 변수를 알려 줍니다.
- `[오류] 업뎃 봇(@update)을 Slack에 연결하지 못했습니다`: 이름이 나온 봇의 두 토큰을 확인하세요.
- `invalid_auth`: 토큰이 틀렸거나 `xoxb-`와 `xapp-` 토큰을 서로 바꿔 넣었습니다.
- `not_in_channel` / `channel_not_found`: 채널 ID를 확인하고 `/invite @moongchi`(업뎃은 `@update`, 일정은 `@schedule`)로
  그 봇을 초대하세요.
- 멘션해도 아무 반응이 없으면 봇 프로그램이 켜져 있는지(서비스면 `python -m mungchi service status`),
  그 채널에 봇이 초대되어 있는지 확인하세요. 봇이 꺼져 있을 때 보낸 메시지는 나중에 처리되지 않을 수 있습니다.
- 어떤 멘션은 답하고 어떤 멘션은 답이 없으면 같은 봇이 두 곳에서 돌고 있을 수 있습니다(예: 서비스와 터미널 탭).
  `python -m mungchi service status`가 "터미널에서 직접 띄운 봇도 돌고 있습니다"라고 하면 그 터미널 탭에서 Ctrl+C로 끄세요.

## 백그라운드로 실행하기 (추천)

터미널 탭을 열어 두지 않아도 Slack 봇(고뭉치·업뎃·일정)이 계속 돌도록 macOS 서비스로 설치합니다(macOS 전용).
한 번 설치하면 **로그인할 때 자동으로 켜지고**, 봇이 죽으면 **자동으로 다시 켜집니다**(최소 30초 간격).

**왜 앱으로 감싸나요?** macOS는 캘린더 권한을 프로그램을 띄운 **앱**에 줍니다. 터미널에서 실행하면 터미널이 권한을 받지만,
launchd(macOS의 서비스 관리자)가 Python을 바로 띄우면 권한을 받을 앱이 없습니다. 그래서 설치할 때 작은 앱
`~/Applications/MungchiBot.app`("비서실 고뭉치", Dock에는 보이지 않음)을 만들고 봇을 그 안에서 실행합니다.
캘린더 권한은 이 앱이 받습니다. launchd(`~/Library/LaunchAgents/local.mungchi.bot.plist`)는 로그인할 때와 봇이 멈췄을 때
이 앱을 띄우는 일만 합니다.

> **먼저 Terminal 탭에서 돌리던 봇은 Ctrl+C로 끄고 설치하세요.** 같은 봇이 두 곳에서 Slack에 연결하면 이벤트가 두 프로세스로
> 나뉘어 어떤 멘션은 답이 없습니다. 설치할 때 터미널에서 띄운 봇이 보이면 경고해 줍니다.

아래 명령은 모두 저장소 폴더에서 가상환경을 켠 상태(`(.venv)`)로 실행합니다.

1. **설치** (한 번만)
   ```bash
   python -m mungchi service install
   ```
   설치 전에 가상환경, `.env`, Slack 설정(`python -m mungchi slack`과 같은 검사)을 확인하고, 문제가 있으면 설치하지 않고 알려 줍니다.
   설치가 끝나고 봇이 시작하면 곧 **"비서실 고뭉치"의 캘린더 접근 확인 창**이 뜹니다. **허용**을 누르세요
   (창에 `MungchiBot`으로 나올 수도 있습니다). 터미널에 줬던 권한은 이 앱으로 넘어가지 않아서 한 번 더 허용해야 합니다.
   Mac 캘린더 앱 대신 ICS 주소를 읽도록 설정했다면 창은 뜨지 않습니다.
2. **상태 보기**
   ```bash
   python -m mungchi service status
   ```
   설치·launchd 등록 여부, 봇 프로세스 번호(PID), 서비스 앱이 시작할 때 확인한 캘린더 권한,
   [아침 브리핑](#아침-브리핑-매일-자동으로-받기) 시각과 마지막으로 보낸 날(`last_brief_date`), 최근 로그 10줄이 나옵니다.
   (캘린더 권한은 `status`를 실행한 터미널의 권한이 아니라 서비스 앱의 권한입니다.)
3. **로그 보기**
   ```bash
   python -m mungchi service logs          # 마지막 50줄
   python -m mungchi service logs -n 200   # 마지막 200줄
   python -m mungchi service logs -f       # 새 로그를 계속 보기 (Ctrl+C로 그만 보기)
   ```
4. **`.env`를 고친 뒤에는 다시 시작**해야 반영됩니다. 코드를 받은 뒤(`git pull`)에도 마찬가지입니다.
   ```bash
   python -m mungchi service restart
   ```
5. **멈추기**
   ```bash
   python -m mungchi service stop
   ```
   다음에 로그인하면 다시 켜집니다. 지금 다시 켜려면 `python -m mungchi service start`.
6. **지우기**
   ```bash
   python -m mungchi service uninstall
   ```
   서비스를 멈추고 앱과 launchd 설정을 지웁니다. 로그는 남겨 둡니다.

- **로그 위치**: `~/Library/Logs/mungchi/bot.log`(봇 로그)와 `~/Library/Logs/mungchi/launchd.log`(launchd가 앱을 띄우다 난 오류).
  봇 로그에도 토큰·키는 남기지 않고, `status`·`logs`로 볼 때 한 번 더 지웁니다. 로그는 저절로 지워지지 않으니
  너무 커지면 서비스를 멈춘 뒤 직접 지우세요.
- **잠자기**: Mac이 잠자기에 들어가면 봇도 멈춥니다. 계속 답하게 하려면 **시스템 설정 → 에너지**에서
  **"디스플레이가 꺼져 있을 때 자동으로 잠자기 방지"** 를 켜 두세요. 잠자는 동안 놓친 아침 브리핑은 깨어난 뒤 보냅니다
  ([Mac이 잠자고 있었다면](#mac이-잠자고-있었다면) 참고).
- **서비스는 터미널에서 `export`한 값을 읽지 못합니다.** 봇에 필요한 값(Claude 키, `SSL_CERT_FILE` 등)은 모두 `.env`에 넣으세요.
  설치할 때 터미널에만 있고 `.env`에는 없는 값이 있으면 이름을 알려 줍니다(값은 출력하지 않습니다).
- 저장소 폴더나 가상환경(`.venv`)을 옮기거나 새로 만들었다면 `install`을 다시 실행하세요. 앱 안에 그 경로가 적혀 있습니다.
  다시 설치하면 macOS가 캘린더 권한을 다시 물을 수 있습니다.
- 설치하면 macOS가 "백그라운드 항목이 추가됨" 알림을 보여 줄 수 있습니다. **시스템 설정 → 일반 → 로그인 항목**에서
  이 항목을 끄면 로그인할 때 서비스가 켜지지 않습니다.

### 서비스 문제 해결

- **캘린더 확인 창이 안 뜨거나 실수로 거부했을 때**
  1. **시스템 설정 → 개인정보 보호 및 보안 → 캘린더**를 엽니다.
  2. 목록에서 **비서실 고뭉치**(또는 **MungchiBot**)를 찾아 **전체 접근**으로 바꿉니다.
  3. `python -m mungchi service restart`를 실행하고, `python -m mungchi service status`의 캘린더 권한이
     "허용됨(전체 접근)"인지 확인합니다.

  목록에 "비서실 고뭉치"도 "MungchiBot"도 없다면 이 방법이 이 Mac에서는 동작하지 않는 것입니다.
  `python -m mungchi service status`와 `python -m mungchi service logs -n 100`의 출력을 함께 알려 주세요.
  그동안은 ICS 주소(캘린더의 방법 B)로 읽거나 터미널 탭에서 `python -m mungchi slack`을 돌리면 됩니다.
- **로그아웃·재시동·시스템 종료가 "비서실 고뭉치"(또는 MungchiBot) 때문에 멈추면**: `python -m mungchi service stop`을
  실행한 뒤 다시 시도하고, 이 일을 알려 주세요(아직 실제 Mac에서 확인하지 못한 부분입니다).
- **봇이 계속 다시 시작하면**: `python -m mungchi service logs`에서 `[오류]` 줄을 보세요. `.env` 설정 오류면 고친 뒤
  `python -m mungchi service restart`. 앱을 띄우는 단계의 오류는 `~/Library/Logs/mungchi/launchd.log`에 남습니다.

### Linux에서는

`service` 명령은 macOS 전용입니다. Linux에서 컴퓨터를 켤 때마다 자동으로 실행하려면 systemd 사용자 서비스를 만듭니다
(`~/.config/systemd/user/mungchi-slack.service`).

```ini
[Unit]
Description=비서실 Slack 봇 (고뭉치·업뎃·일정)

[Service]
WorkingDirectory=/path/to/research
ExecStart=/path/to/research/.venv/bin/python -m mungchi slack
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now mungchi-slack
journalctl --user -u mungchi-slack -f      # 로그 보기
loginctl enable-linger "$USER"             # 로그아웃한 뒤에도 계속 실행하려면
```

## 문제 해결

### 업뎃이 변경을 못 찾을 때

공저자가 `/20_연구-진행`(하위 폴더 포함)에서 파일을 고쳤는데 업뎃이 "공저자 변경 없음"이라고 하면, 함께 붙는 이유 한 줄
(예: "최근 24시간 동안 공저자가 바꾼 파일이 없어요", "지난 브리핑(10/06 07:50) 이후 바뀐 파일이 없어요")을 먼저 보세요.
흔한 원인은 아래와 같습니다.

- **기간 밖입니다.** 기간을 말하지 않으면 업뎃과 고뭉치는 **최근 24시간**만 보고, 브리핑(`--brief`)은 **지난 브리핑 이후**만 봅니다.
  → 기간을 정해 물어보세요: `@업뎃 최근 3일 동안 누가 무슨 파일 고쳤어?` (`오늘`, `이번 주`, `지난 48시간`도 됩니다).
  기간을 말하면 그 기간을 그대로 보고, 브리핑 기준 시각도 바꾸지 않습니다.
- **임시·잠금 파일은 빠집니다.** `~$원고.docx`, `.~lock.…`, `*.tmp`, Stata의 `*.stswp` 같은 파일은 공저자가 저장했어도 알리지 않습니다.
- **누가 고쳤는지 알 수 없는 파일은 빠집니다.** Dropbox는 **공유 폴더 안의 파일**에만 마지막 수정자를 알려 줍니다.
  공유 폴더가 아닌 곳에 있는 파일은 누가 고쳤는지 알 수 없어 보고에서 뺍니다. 공저자와 함께 쓰는 폴더는 Dropbox에서
  공유 폴더로 공유되어 있어야 합니다.
- **내가 마지막으로 저장한 파일은 빠집니다.** 공저자가 고친 뒤 내가 다시 저장하면 마지막 수정자가 나라서 보고하지 않습니다.
- **삭제·이동·이름 바꾸기는 나오지 않습니다.** 옮기거나 이름만 바꾼 파일은 Dropbox의 수정 시각이 그대로라 기간 안에
  들어오지 않고, 지운 파일은 목록에 없습니다.

어느 경우인지 직접 확인하려면 아래 명령을 실행하세요. Claude API를 쓰지 않고(비용 없음), 브리핑 기준 시각도 바꾸지 않습니다.

```bash
python -m mungchi --dropbox-check --hours 72    # 최근 72시간
python -m mungchi --dropbox-check               # 기간 없이 물을 때와 같은 최근 24시간 + 브리핑 기준 시각
```

- 확인할 폴더와 그 폴더가 있는지, 연결된 Dropbox 계정 이름, 기간의 기준과 시작 시각, 훑어본 파일 수(그중 임시 파일 수),
  기간 안에 바뀐 파일 수와 포함·제외 개수를 보여 줍니다.
- `--hours` 없이 실행하면 저장된 **브리핑 기준 시각**(브리핑이 마지막으로 확인한 때, 없으면 `LOOKBACK_DAYS`일)과
  지금 브리핑하면 나올 공저자 파일 수도 함께 보여 줍니다.
- 기간 안에 바뀐 파일(최근 수정 순, 최대 50개)마다 경로, 수정 시각, 수정한 사람, 공유 폴더 여부, 판정
  (포함 / 제외: 내가 수정 / 제외: 수정자 정보 없음(공유 폴더 아님) / 제외: 임시 파일)을 표로 보여 줍니다.
  업뎃과 **같은 규칙**으로 판정하므로 "포함"인 파일이 업뎃이 알려 주는 파일입니다.
- 이어서 기간과 상관없이 가장 최근에 바뀐 파일 10개와 판정을 보여 줍니다. 찾던 파일이 "제외: 기간 이전"으로 나오면
  기간이 문제이니 `--hours`를 늘리거나 업뎃에게 기간을 정해 물어보세요.
- "폴더: 찾을 수 없음"이 나오면 `DROPBOX_ROOT_FOLDER`에 Dropbox 맨 위부터의 전체 경로를 적었는지 확인하세요.
  Dropbox 팀 계정(팀 스페이스)을 쓰면 웹에서 보이는 경로와 다를 수 있습니다(출력의 `→` 안내 참고).
- 토큰·키는 출력하지 않습니다.

### `zsh: command not found: python`

가상환경이 꺼져 있습니다(Mac에는 `python3`만 있고, `python`은 가상환경 안에만 있습니다).
저장소 폴더에서 가상환경을 켜고 다시 실행하세요. 프롬프트 앞에 `(.venv)`가 붙어야 합니다.

```bash
cd research
source .venv/bin/activate
```

`.venv` 폴더가 없다는 오류가 나면 아직 가상환경을 만들지 않은 것이니 [설치](#설치)의 `python3 -m venv .venv`부터 하세요.

### `CERTIFICATE_VERIFY_FAILED` (`unable to get local issuer certificate`)

예: 처음 Slack에 연결할 때 `ClientConnectorCertificateError ... CERTIFICATE_VERIFY_FAILED`.
python.org에서 받은 macOS용 Python은 인증서(CA) 묶음 없이 설치되기 때문입니다. 설치한 버전 폴더의
`Install Certificates.command`를 한 번 실행하세요(`/Applications/Python 3.x/Install Certificates.command`에서
`3.x`는 설치한 버전입니다. Finder의 응용 프로그램 폴더에서 더블클릭해도 됩니다).

```bash
open "/Applications/Python 3.14/Install Certificates.command"    # Python 3.14를 설치했다면
```

그래도 안 되면 가상환경을 켠 상태에서 인증서 파일을 직접 지정합니다(터미널을 열 때마다, 또는 `~/.zshrc`에 추가).

```bash
export SSL_CERT_FILE="$(python -m certifi)"
```

[백그라운드 서비스](#백그라운드로-실행하기-추천)는 `~/.zshrc`의 `export`를 읽지 못합니다. 서비스에서도 쓰려면
`python -m certifi`가 출력한 경로를 `.env`에 `SSL_CERT_FILE=<그 경로>`로 넣고 `python -m mungchi service restart`를 실행하세요.

### `There's an issue with the selected model (...)`

고뭉치는 이때 `[오류] 모델 설정에 문제가 있습니다.`와 함께 `→ .env의 MUNGCHI_MODEL과 ANTHROPIC_BASE_URL을 확인하세요`를 보여 줍니다.

- `MUNGCHI_MODEL`이 그 API(또는 게이트웨이)에 없는 모델 ID입니다. `python -m mungchi --list-models`로 목록을 보고
  맞는 ID를 넣으세요.
- 게이트웨이를 쓴다면 `ANTHROPIC_BASE_URL` 경로도 확인하세요. Chat KHU는 화면에 보이는 `.../v1/gateway`가 아니라
  `.../v1/gateway/claude`입니다.

### 401 / `authentication_error` / `[오류] 인증에 실패했습니다.`

- Anthropic API 키를 쓴다면 `ANTHROPIC_API_KEY` 값이 맞는지 확인하세요.
- 게이트웨이를 쓴다면 `ANTHROPIC_AUTH_TOKEN`에 게이트웨이 키를 넣고, **`ANTHROPIC_API_KEY`는 비워 두세요**.
  셸에서 `export`해 둔 값이 있으면 `unset ANTHROPIC_API_KEY`로 지웁니다.
- `python -m mungchi --list-models`로 키가 통하는지 에이전트를 실행하지 않고 바로 확인할 수 있습니다.

## 보안

- 모든 도구는 읽기 전용입니다. Dropbox·캘린더의 내용을 바꾸지 않습니다.
  Mac 캘린더 앱은 macOS가 읽기에 '전체 접근'을 요구해서 그 권한을 받지만, 고뭉치는 일정을 읽기만 합니다.
- 토큰·키와 캘린더 주소(Google 비공개 주소, iCloud 공개 캘린더 주소)는 도구 출력·오류 메시지·로그에 나오지 않도록 지웁니다(`***`).
- 고뭉치는 Bash·파일 쓰기 같은 내장 도구를 쓸 수 없고, 데이터 도구도 직접 부를 수 없습니다.
  업뎃은 Dropbox 도구만, 일정은 캘린더·날씨 도구만 쓸 수 있습니다(PreToolUse 훅으로 강제).
- 업뎃·일정을 직접 부를 때(Slack 봇, `--agent`)는 내장 도구와 Agent 도구가 아예 없고, 자기 데이터 도구만 보이고
  쓸 수 있습니다. 다른 담당자의 도구를 부르려 해도 PreToolUse 훅이 막습니다.
- 사용자 설정 파일(`~/.claude/settings.json` 등)은 읽지 않아 도구 구성이 바뀌지 않습니다.
- 세 Slack 봇 모두 `SLACK_ALLOWED_USER_IDS`에 있는 사람의 메시지만 에이전트에게 넘기고, 이 값이 비어 있으면 시작하지 않습니다.
  봇 자신이나 우리 봇들끼리, 다른 봇의 메시지, 수정·입장 같은 시스템 메시지, 중복으로 들어온 이벤트는 무시합니다.
- Slack에 올리는 오류 메시지에는 오류 종류와 확인할 설정만 적습니다(Claude API 오류는 API가 준 오류 문구 일부도 함께).
  프로그램 오류의 자세한 내용은 봇을 실행한 터미널(표준 오류)에만 남기고(백그라운드 서비스면 `~/Library/Logs/mungchi/bot.log`),
  Slack 토큰·Claude 키를 포함한 비밀값은 어디에서나 지웁니다.
- 게이트웨이(방법 2)를 쓰면 에이전트가 읽은 데이터가 게이트웨이 운영 기관을 거쳐 갑니다. 이용 정책을 확인하세요.
- 크레딧 확인(`--credits`, Slack의 크레딧 답, 잔액 알림)은 게이트웨이의 크레딧 조회 주소만 부르고 모델은 부르지 않습니다.
  키는 요청 헤더에만 넣고 출력·Slack·로그에는 남기지 않습니다. Slack에서 묻더라도 허용 목록 확인이 먼저입니다.
- 날씨(`--weather`, 브리핑의 날씨 줄, Slack의 날씨 답)는 Open-Meteo만 부르고 모델은 부르지 않습니다. 키는 없고,
  보내는 것은 설정한 위도·경도와 시간대뿐입니다. Slack에서 묻더라도 허용 목록 확인이 먼저입니다.
  '일정'의 날씨 도구(`get_weather`)도 Open-Meteo만 부르지만, 에이전트가 쓰는 도구라 그 답에는 모델 호출이 들어갑니다.
- Slack에 올리는 답에서는 `@channel`·`@here` 같은 전체 알림을 막고, 링크 미리보기(unfurl)를 끕니다.

## 알려진 한계

- **Dropbox의 `modified_by`(마지막 수정자)는 공유 폴더 안의 파일에만 있습니다.**
  공유되지 않은 폴더의 파일은 누가 고쳤는지 알 수 없어 보고에서 빠집니다.
  공유 폴더인데도 수정자 정보가 없으면 "확인 불가"로 표시합니다.
- Dropbox는 **어떤 파일이 바뀌었는지만** 알려 주고, 무엇을 고쳤는지는 알려 주지 않습니다(토큰 절약).
  또 마지막 수정자 기준이라, 공저자가 고친 뒤 내가 다시 저장하면 마지막 수정자가 나라서 빠집니다.
- Dropbox의 **삭제·이동·이름 바꾸기는 감지하지 않습니다**(수정 시각이 바뀌는 파일 수정만 봅니다).
- **알림은 터미널(표준 출력)과 Slack으로 받을 수 있습니다.** 이메일 전달은 아직 없습니다
  (`python -m mungchi --brief`의 출력을 메일로 보내는 식으로 붙일 수 있습니다).
- Slack 채널에서는 권한을 최소로 하려고 채널 메시지 읽기 권한을 받지 않습니다. 그래서 봇은 멘션한 메시지만 보고,
  스레드의 다른 메시지(다른 봇의 답 포함)는 읽지 않습니다. 대화도 봇마다 따로라서, 업뎃 봇에게 들은 내용을 고뭉치는 모릅니다.
- Dropbox의 브리핑 기준 시각은 브리핑(아침 브리핑, `--brief`, `--brief --slack`)만 바꿉니다. 브리핑 사이에 업뎃이나 고뭉치에게
  기간 없이 물으면 최근 24시간만 보니, 그보다 앞의 변경은 기간을 말해서 물어보세요.
- 아침 브리핑은 Slack 봇이 돌고 있을 때만 갑니다. 그 시각에 Mac이 잠자고 있으면 깨어난 뒤 `BRIEF_CATCHUP_UNTIL`(기본 12:00) 전까지만
  따라잡아 보내고, 그 뒤에는 그날을 건너뜁니다.
- **Mac 캘린더 앱 권한은 Python을 실행한 앱에 주어집니다.** 터미널에서 띄운 봇은 터미널의 권한을,
  [백그라운드 서비스](#백그라운드로-실행하기-추천)로 띄운 봇은 서비스 앱("비서실 고뭉치")의 권한을 씁니다. 서로 넘어가지 않으니
  각각 허용해야 하고, iTerm 같은 다른 터미널 앱도 따로 허용해야 합니다. 아침 브리핑은 봇 안에서 돌므로 봇의 권한
  (서비스면 서비스 앱의 권한)을 씁니다. cron으로 따로 돌리는 브리핑(`--brief --slack`)은 어느 쪽 권한도 쓰지 못할 수 있으니
  (실제로는 확인하지 못했습니다) 아침 브리핑(`BRIEF_TIME`)을 쓰거나 ICS 주소(방법 B)로 읽으세요.
- 백그라운드 서비스(앱 + launchd)는 이 저장소의 테스트로는 명령 순서와 생성 파일만 확인했고, 실제 Mac에서의 동작
  (캘린더 확인 창, 로그인 시 자동 시작, 로그아웃·재시동)은 아직 확인하지 못했습니다. 이상하면
  [서비스 문제 해결](#서비스-문제-해결)을 보고 알려 주세요.
- 날씨 줄은 Open-Meteo 공개 API의 변수 이름(`temperature_2m`, `weather_code`, `temperature_2m_max`·`_min`,
  `precipitation_probability_max`, `pm10`, `pm2_5`)으로 받습니다. 개발 환경에서는 Open-Meteo에 접속할 수 없어 실제 응답으로는
  확인하지 못했고 가짜 응답으로만 테스트했습니다. 날씨가 계속 "가져오지 못했어요"로 나오면 `python -m mungchi --weather`로 이유를 확인하세요.
- 고뭉치에게 물으면 고뭉치·업뎃·일정이 모두 모델을 호출하므로 API 비용이 듭니다. 업뎃·일정을 직접 부르면 한 에이전트만
  호출합니다. Slack 멘션·DM도 한 번마다 비용이 듭니다.

## 개발

```bash
pip install -e '.[dev]'
pytest -q
```

테스트는 네트워크를 쓰지 않습니다. Dropbox 클라이언트는 가짜 객체로 대신하고,
캘린더는 테스트 안의 ICS 문자열과 고정된 시계로 확인합니다. Mac 캘린더 앱(EventKit)은 가짜 어댑터와 가짜 EventKit 객체로
확인하므로 macOS가 아니어도 테스트가 돌고, 실제 캘린더 앱에는 접근하지 않습니다. Slack은 가짜 웹 클라이언트와 가짜 `run_turn`으로
확인하므로 실제 Slack이나 Claude에 연결하지 않습니다. 아침 브리핑의 시각 판단과 스케줄러는 가짜 시계로 확인합니다.
`--list-models`와 크레딧 확인(`--credits`, Slack의 크레딧 답, 잔액 알림), 날씨(`--weather`, 브리핑의 날씨 줄, Slack의 날씨 답, `get_weather`)는
가짜 httpx 전송(`MockTransport`)과 고정된 시계로 확인합니다. 가짜 전송 없이 나가는 httpx 요청은 테스트에서 연결 실패로 바뀝니다.
백그라운드 서비스(`service`)는 가짜 명령 실행기로 확인하므로 `osacompile`·`codesign`·`launchctl`·`pgrep`·`pkill`을 실제로
부르지 않습니다(`run-bot.sh`만 bash와 가짜 Python으로 직접 실행해 따옴표 처리를 확인합니다).

```
slack_manifests/       # Slack 앱 매니페스트, 봇마다 하나 (moongchi.yaml · update.yaml · schedule.yaml)
src/mungchi/
├── __main__.py        # python -m mungchi
├── main.py            # 페르소나별 ClaudeAgentOptions 구성, 한 턴 실행(run_turn), CLI(--agent 포함), 출력 스트리밍, 오류 문구
├── briefing.py        # 오늘 브리핑(--brief, --brief --slack, 아침 브리핑 공통): 제목·날씨·고뭉치 답·크레딧 조립, 아침 브리핑 시각 판단
├── model_list.py      # --list-models: 모델 목록 확인 (GET /v1/models)
├── credits.py         # --credits, Slack의 크레딧 바로 답, 잔액 알림 문구: Chat KHU 크레딧·사용량 조회 (LLM 호출 없음)
├── weather.py         # --weather, 브리핑의 날씨 줄, Slack의 날씨 바로 답(질문 판별 포함), get_weather의 JSON: Open-Meteo 날씨·미세먼지 (키·LLM 호출 없음)
├── calendar_setup.py  # --calendar-setup: Mac 캘린더 앱 접근 허용, 캘린더·오늘 일정 확인
├── dropbox_check.py   # --dropbox-check: 업뎃이 Dropbox 변경을 못 찾을 때 원인 확인 (읽기 전용)
├── service.py         # service: macOS 백그라운드 서비스 (AppleScript 앱 + LaunchAgent), 상태·로그 보기
├── personas.py        # 페르소나 키(mungchi·update·schedule), 한글 이름, Slack 핸들
├── slack_bot.py       # Slack 봇 세 개(한 프로세스, Socket Mode), 권한 확인, 진행 표시, 크레딧·날씨 바로 답, 잔액 알림, 아침 브리핑, --brief --slack
├── slack_format.py    # Slack용 프롬프트, 봇별 첫 답, 멘션 제거, mrkdwn 변환, 메시지 나누기
├── agents.py          # 고뭉치 프롬프트, 업뎃·일정 프롬프트(하위 에이전트용·직접 대화용), AgentDefinition, 도구 권한 훅
├── config.py          # 환경변수 읽기(봇별 Slack 토큰 포함), 설정 누락 안내 문구
├── state.py           # 브리핑 기준 시각·크레딧 알림·아침 브리핑 날짜 기록(.mungchi_state.json), 봇별 Slack 스레드↔대화(.mungchi_slack_threads.json)
└── tools/
    ├── __init__.py    # SDK MCP 서버(mungchi)와 도구 이름
    ├── common.py      # 비밀값 지우기, 인자 정리, 결과 JSON
    ├── dropbox_tool.py
    ├── calendar_tool.py   # get_schedule: Mac 캘린더 앱 또는 ICS 주소에서 일정 읽기
    ├── weather_tool.py    # get_weather: '일정'의 날씨 도구 (weather.py로 오늘·내일 날씨를 JSON으로)
    └── macos_calendar.py  # EventKit 어댑터 (pyobjc는 Mac에서 필요할 때만 불러옴)
```
