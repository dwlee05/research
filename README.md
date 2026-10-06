# 고뭉치 비서실

공저자들이 Dropbox와 Overleaf에서 어떤 파일과 프로젝트를 고쳤는지, 오늘·내일 일정이 어떤지를 한 번에 챙겨 주는
연구자용 비서입니다. [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview)
(`claude-agent-sdk`)로 만든 멀티 에이전트 프로그램입니다.

## 구조

```
사용자
 ├─ 터미널 · cron 브리핑 · Slack @moongchi (고뭉치 봇)
 │   └─ 비서실장 고뭉치 ─ main 에이전트. 데이터에 직접 손대지 않고 Agent 도구로 일을 맡김
 │       ├─ 업뎃 (update) ─ 공저자 업데이트 담당
 │       │    ├─ check_dropbox_updates  → Dropbox 폴더(20_연구-진행)의 공저자 변경 파일 목록 (내용은 안 읽음)
 │       │    └─ check_overleaf_updates → Overleaf 프로젝트별로 누가 언제 몇 번 편집했는지 (내용은 안 읽음, git log)
 │       └─ 일정 (schedule) ─ 캘린더 일정 담당
 │            └─ get_schedule           → ICS 캘린더 (Google·Outlook·iCloud)
 ├─ Slack @update (업뎃 봇) · 터미널 --agent update     → 업뎃이 바로 답함 (Dropbox·Overleaf 도구만)
 └─ Slack @schedule (일정 봇) · 터미널 --agent schedule → '일정'이 바로 답함 (캘린더 도구만)
```

- **고뭉치**는 브리핑을 부탁받으면 업뎃과 '일정'에게 **동시에** 일을 맡기고, 두 보고를 합쳐
  ① 공저자 업데이트 ② 일정 ③ 오늘 챙길 것 세 부분으로 된 브리핑을 씁니다.
  고뭉치가 쓸 수 있는 도구는 Agent(하위 에이전트 호출) 하나뿐입니다.
- **업뎃**은 Dropbox·Overleaf 도구만 씁니다. 사용자 본인의 작업은 빼고 **공저자의 작업만** 보고합니다.
  - Dropbox: `20_연구-진행` 폴더에서 공저자가 바꾼 **파일 목록만** 하위 폴더·사람별로 수정 시각과 폴더 링크를 붙여 알려 줍니다.
    토큰을 아끼려고 파일 내용은 읽지도 요약하지도 않으니, 내용은 직접 열어 확인하세요.
  - Overleaf: 공저자가 편집한 **프로젝트 목록만** 알려 줍니다. 프로젝트마다 링크를 붙이고, 누가 마지막으로 언제
    편집했는지와 편집 횟수를 적습니다. 역시 토큰을 아끼려고 원고 내용(diff)은 읽지도 요약하지도 않습니다.
- **일정**('일정' 에이전트)은 캘린더 도구만 씁니다. 그날과 다음 날 일정, "지금 / 바로 다음 일정", 겹침과 빈 시간을 짧게 보고합니다.
- 업뎃과 일정은 고뭉치를 거치지 않고 **직접** 부를 수도 있습니다. Slack에서는 각자의 봇(`@update`, `@schedule`)을,
  터미널에서는 `--agent update` / `--agent schedule`을 씁니다. 이때도 자기 도구만 쓰고, 다른 도구나 Agent 도구는 쓸 수 없습니다.
- 세 도구는 모두 **읽기 전용**이고, 프로그램 안에서 도는 SDK MCP 서버(`mungchi`)로 묶여 있습니다.
- 모델은 `MUNGCHI_MODEL`(기본 `claude-opus-5-5`)이며, 업뎃·일정은 같은 모델을 이어받습니다(`inherit`).
- 터미널과 Slack은 같은 에이전트를 씁니다. Slack에서 부르는 방법은 아래 [Slack에서 부르기](#slack에서-부르기)를 보세요.

## 설치

Python 3.10 이상과 `git`이 필요합니다.

```bash
git clone <이 저장소 주소> research
cd research
python3 -m venv .venv
source .venv/bin/activate
pip install -e .            # 개발/테스트까지: pip install -e '.[dev]'
cp .env.example .env        # 그다음 .env를 채웁니다 (아래 참고)
```

Claude 인증은 둘 중 하나면 됩니다.

- `.env`의 `ANTHROPIC_API_KEY`에 [Claude Console](https://console.anthropic.com)에서 만든 API 키를 넣거나,
- Claude Code CLI로 미리 로그인해 둡니다(`claude` 실행 후 `/login`).

> 실행할 때마다 Claude API를 호출하므로 사용량에 따라 비용이 듭니다.

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

### 2. Overleaf

1. Overleaf의 **Git 연동은 유료 플랜(또는 기관 프리미엄) 기능**입니다. 먼저 계정에서 쓸 수 있는지 확인하세요.
2. Overleaf → **Account Settings** → **Git Integration** → **Generate token** →
   `OVERLEAF_GIT_TOKEN`에 넣습니다(토큰은 만들 때 한 번만 보입니다).
3. 프로젝트 ID는 프로젝트 주소 `https://www.overleaf.com/project/<프로젝트ID>`의 끝부분입니다
   (프로젝트 메뉴 → Git에 나오는 `https://git.overleaf.com/<프로젝트ID>`와 같습니다).
   `OVERLEAF_PROJECTS=논문A=<ID>,논문B=<ID>`처럼 쉼표로 구분해 적습니다. 이름 없이 ID만 써도 됩니다.
4. `MY_NAMES`와 `MY_EMAILS`에 Overleaf에 표시되는 내 이름과 이메일을 적습니다(쉼표 구분, 대소문자 무시).
   이 값으로 내 커밋을 걸러 내므로 **둘 중 하나 이상은 꼭** 채워야 합니다.

고뭉치는 Overleaf에서 **편집 기록(누가, 언제, 몇 번)만** 읽습니다(`git log`의 작성자 이름·이메일·시각).
토큰을 아끼려고 원고 내용과 diff는 읽지 않습니다.

고뭉치는 토큰을 `git -c http.extraHeader=...`로 명령마다 넘기므로 `.git/config`에 토큰이 저장되지 않습니다.
받은 프로젝트는 `~/.cache/mungchi/overleaf/<프로젝트ID>`에 보관됩니다(`OVERLEAF_CACHE_DIR`로 변경 가능).

### 3. 캘린더 (Google 캘린더의 비공개 iCal 주소)

OAuth 없이 ICS 주소만으로 읽습니다.

1. 컴퓨터에서 [Google 캘린더](https://calendar.google.com) → 오른쪽 위 톱니바퀴 → **설정**.
2. 왼쪽 **내 캘린더의 설정**에서 캘린더를 고릅니다.
3. **캘린더 통합** 항목의 **iCal 형식의 비공개 주소**(비공개 주소, iCal 형식)를 복사해 `CALENDAR_ICS_URLS`에 넣습니다.
   캘린더가 여러 개면 쉼표로 구분합니다. (회사·학교 계정은 관리자가 이 기능을 꺼 두었을 수 있습니다.)

- Outlook: 설정 → 캘린더 → 공유 캘린더 → **캘린더 게시**에서 ICS 링크.
- iCloud: 캘린더 공유 설정의 **공개 캘린더** 링크(`webcal://`도 그대로 쓸 수 있음).

> 비공개 주소는 **비밀번호와 같습니다**. 유출됐다면 같은 화면에서 재설정하세요.
> 고뭉치는 이 주소를 출력이나 오류 메시지에 내보내지 않습니다.

## 사용법

```bash
# 대화 모드: 여러 번 주고받기. exit 또는 종료 를 입력하면 끝납니다.
python -m mungchi

# 오늘 브리핑 한 번 (① 공저자 업데이트 ② 일정 ③ 오늘 챙길 것)
python -m mungchi --brief

# 질문 한 번
python -m mungchi "지난 48시간 동안 Overleaf에서 누가 어느 프로젝트를 고쳤어?"
python -m mungchi "내일 오후에 비는 시간 있어?"

# 고뭉치를 거치지 않고 업뎃이나 '일정'에게 바로 묻기 (질문 없이 쓰면 대화 모드)
python -m mungchi --agent update "지난 48시간 동안 Overleaf 누가 고쳤어?"
python -m mungchi --agent schedule

# Slack 봇 실행 / 오늘 브리핑을 Slack에 올리기 (아래 "Slack에서 부르기" 참고)
python -m mungchi slack
python -m mungchi --brief --slack

# 도움말
python -m mungchi --help
```

`pip install -e .`를 했다면 `python -m mungchi` 대신 `mungchi`로 실행해도 됩니다.
고뭉치의 답은 표준 출력(stdout)으로 흘러나오고, `→ 업뎃에게 맡기는 중...` 같은 진행 표시는
표준 오류(stderr)로 나옵니다. 그래서 `python -m mungchi --brief > 오늘.md`처럼 브리핑만 파일로 저장할 수 있습니다.

### 확인 범위

- 업뎃의 도구는 기본적으로 **마지막으로 확인한 시각 이후**의 변경만 봅니다.
  확인 시각은 소스(Dropbox, Overleaf 프로젝트별)마다 `.mungchi_state.json`에 저장됩니다(`MUNGCHI_STATE_FILE`로 경로 변경).
- 기록이 없으면 최근 `LOOKBACK_DAYS`일(기본 7일)을 봅니다.
- "지난 48시간"처럼 기간을 말하면 그 범위로 봅니다.
- Dropbox는 파일 목록만 봅니다. 한 번에 최근 60개 파일까지 이름과 수정 시각을 적고,
  그보다 많으면 나머지는 하위 폴더·사람별 개수만 알려 줍니다.
- Overleaf도 목록만 봅니다. 공저자가 편집한 프로젝트를 최근 편집 순으로, 사람마다 마지막 편집 시각과
  편집 횟수(커밋 수)만 알려 주고, 변경 없는 프로젝트는 한 줄로 묶습니다. 원고 내용은 직접 열어 확인하세요.

## 매일 자동으로 받기 (cron)

`crontab -e`로 아래 줄을 추가하면 평일 아침 7시 50분에 브리핑이 Slack 채널에 올라갑니다
(Slack 설정은 아래 [Slack에서 부르기](#slack에서-부르기) 참고).

```cron
# 시간대는 시스템 기준입니다. git이 PATH에 있어야 Overleaf 확인이 됩니다.
PATH=/usr/local/bin:/usr/bin:/bin
50 7 * * 1-5  cd /path/to/research && .venv/bin/python -m mungchi --brief --slack >> "$HOME/mungchi-briefing.log" 2>&1
```

Slack 없이 파일에 쌓으려면 `--slack`을 빼면 됩니다(브리핑은 로그 파일에 들어갑니다).

- cron에서는 `cd`로 저장소 폴더에 들어가야 `.env`와 `.mungchi_state.json`을 찾습니다.
- cron에서는 Claude 로그인 정보(키체인)를 못 읽을 수 있으니 `.env`에 `ANTHROPIC_API_KEY`를 넣어 두는 편이 안전합니다.
- Dropbox는 몇 시간 뒤 만료되는 액세스 토큰 대신 리프레시 토큰 방식을 쓰세요.

## Slack에서 부르기

내 Slack 워크스페이스에 봇 **세 개**를 따로 들일 수 있습니다. 봇마다 Slack 앱을 하나씩 만들고,
쓰고 싶은 봇만 만들어도 됩니다.

- **고뭉치** (`@moongchi`, 앱 이름 "비서실 고뭉치"): 비서실장. 업뎃과 '일정'에게 일을 맡겨 브리핑하고,
  무엇을 물어도 알맞은 팀원에게 맡깁니다.
  - 예: `@고뭉치`(내용 없이 멘션만 하면 오늘 브리핑), `@고뭉치 내일 오후에 논문A 검토할 시간 있어?`
- **업뎃** (`@update`, 앱 이름 "업뎃"): 공저자가 Dropbox·Overleaf에서 바꾼 파일·프로젝트 목록을 바로 알려 줍니다.
  - 예: `@업뎃`(멘션만 하면 "공저자 업데이트 확인해줘"), `@업뎃 지난 48시간 동안 Overleaf 누가 고쳤어?`
- **일정** (`@schedule`, 앱 이름 "일정"): 캘린더 일정을 바로 알려 줍니다.
  - 예: `@일정`(멘션만 하면 "오늘과 내일 일정 알려줘"), `@일정 금요일 오후에 비는 시간 있어?`

세 봇은 모두 **한 프로세스**(`python -m mungchi slack`)에서 함께 돌아가고, 토큰을 넣은 봇만 켜집니다.
봇은 내 컴퓨터에서 **Socket Mode**로 돌기 때문에 공개 URL이나 서버가 필요 없습니다.
**브리핑 올리기**(`python -m mungchi --brief --slack`)는 고뭉치의 오늘 브리핑을 정해 둔 채널에 올립니다(cron용, 고뭉치 봇 토큰 사용).

> **비용 주의**: 어느 봇이든 멘션이나 DM 한 번마다 Claude API를 호출합니다. 고뭉치는 업뎃·일정까지 모델을 부르고,
> 업뎃·일정 봇은 자기 에이전트 하나만 부릅니다. 동시에 처리하는 요청 수는 세 봇을 합쳐
> `SLACK_MAX_CONCURRENT`(기본 2)로 제한합니다.

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

세 봇 모두 Dropbox·Overleaf·캘린더의 개인 정보를 읽으므로 **`SLACK_ALLOWED_USER_IDS`에 적힌 사람에게만** 답합니다.
이 값은 세 봇이 함께 쓰고, 비어 있으면 봇이 아예 시작하지 않습니다. 다른 사람이 봇을 부르면 스레드에
"이 봇은 소유자만 사용할 수 있어요"라고 한 번만 답하고, 에이전트는 실행하지 않습니다.

1. Slack에서 내 프로필 사진 → **프로필** → **⋮** → **멤버 ID 복사**.
2. `U`로 시작하는 값을 `SLACK_ALLOWED_USER_IDS`에 넣습니다. 여러 명이면 쉼표로 구분합니다.

### 6. 채널 ID 찾고 봇 초대하기

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
- **업뎃 / 일정**: `@업뎃 Overleaf 누가 고쳤어?`, `@일정 내일 일정 알려줘`처럼 부르면 "📝 업뎃이 확인 중이에요...",
  "⏰ 일정이 확인 중이에요..."가 떴다가 답으로 바뀝니다. 고뭉치를 거치지 않으니 진행 상황 표시 없이 바로 답합니다.
- **내용 없이 멘션만** 하면 고뭉치는 오늘 브리핑, 업뎃은 "공저자 업데이트 확인해줘", 일정은 "오늘과 내일 일정 알려줘"로 알아듣습니다.
- **이어서 묻기**: 같은 스레드에서 같은 봇을 다시 멘션하면 그 봇과의 앞 대화를 이어 갑니다. 대화는 봇마다 따로라서,
  업뎃과 이야기하던 스레드에서 고뭉치를 부르면 고뭉치는 새 대화로 시작합니다. 채널에서는 멘션한 메시지만 봇에게
  전달되므로 스레드 안에서도 멘션을 붙여야 합니다.
- **DM**: 각 앱의 **메시지** 탭에서 그냥 보내면 됩니다. 답은 보낸 메시지의 스레드로 오고, 그 스레드에 답장하면 이어 갑니다.
- 스레드와 대화의 연결은 상태 파일과 같은 폴더의 `.mungchi_slack_threads.json`에 봇별로(세 봇 합쳐 최근 200개까지)
  저장되어, 봇을 다시 켜도 이어집니다. 예전 버전이 저장한 연결은 고뭉치 것으로 이어집니다. 대화 기록은 Claude Code가
  `~/.claude/projects/` 아래에 폴더별로 저장하므로 **봇과 cron은 항상 같은 폴더(저장소)에서 실행**하세요.
- 공개 채널에서 부르면 답(공저자 작업, 일정)도 그 채널 사람들이 봅니다. DM이나 비공개 채널을 쓰세요.

**계속 켜 두기**. 가장 간단한 방법은 tmux입니다.

```bash
tmux new -s mungchi
cd /path/to/research && .venv/bin/python -m mungchi slack
# Ctrl+B 다음 D로 빠져나와도 계속 실행됩니다. 다시 보기: tmux attach -t mungchi
```

`nohup .venv/bin/python -m mungchi slack >> "$HOME/mungchi-slack.log" 2>&1 &`도 됩니다.
Linux에서 컴퓨터를 켤 때마다 자동으로 실행하려면 systemd 사용자 서비스를 만듭니다
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

macOS에서는 tmux를 쓰거나, 같은 명령을 launchd(`~/Library/LaunchAgents`)에 등록하면 됩니다.

### 8. 브리핑을 Slack으로 받기

```bash
python -m mungchi --brief --slack
```

- 첫 메시지에 굵은 제목 "☀️ 오늘의 브리핑 (2026-10-05)"(날짜는 `TIMEZONE` 기준)과 브리핑 본문이 함께 올라가서
  채널에서 바로 읽을 수 있습니다. 본문이 길면(약 3,500자 초과) 나머지는 그 메시지의 스레드에 이어 붙습니다.
- 봇이 켜져 있으면 그 스레드에서 `@고뭉치 첫 번째 항목 자세히 알려줘`처럼 고뭉치를 멘션해 브리핑에 이어서 물을 수 있습니다.
- 브리핑을 만들지 못하면 채널에 실패 메시지를 올리고, 0이 아닌 종료 코드로 끝납니다(자세한 내용은 cron 로그에 남습니다).
- `--slack` 없이 `--brief`만 쓰면 예전처럼 터미널(표준 출력)로 나옵니다. cron 설정은 위의 [매일 자동으로 받기](#매일-자동으로-받기-cron)를 보세요.

### 문제 해결

- `[오류] Slack 봇을 시작할 수 없습니다.`: 빠진 환경변수 이름이 함께 나옵니다. `.env`를 채우세요.
  토큰을 하나만 넣은 봇이 있으면 그 봇의 빠진 변수를 알려 줍니다.
- `[오류] 업뎃 봇(@update)을 Slack에 연결하지 못했습니다`: 이름이 나온 봇의 두 토큰을 확인하세요.
- `invalid_auth`: 토큰이 틀렸거나 `xoxb-`와 `xapp-` 토큰을 서로 바꿔 넣었습니다.
- `not_in_channel` / `channel_not_found`: 채널 ID를 확인하고 `/invite @moongchi`(업뎃은 `@update`, 일정은 `@schedule`)로
  그 봇을 초대하세요.
- 멘션해도 아무 반응이 없으면 봇 프로그램이 켜져 있는지, 그 채널에 봇이 초대되어 있는지 확인하세요.
  봇이 꺼져 있을 때 보낸 메시지는 나중에 처리되지 않을 수 있습니다.

## 보안

- 모든 도구는 읽기 전용입니다. Dropbox·Overleaf·캘린더의 내용을 바꾸지 않습니다.
- 토큰과 비공개 캘린더 주소는 도구 출력·오류 메시지·로그에 나오지 않도록 지웁니다(`***`).
- 고뭉치는 Bash·파일 쓰기 같은 내장 도구를 쓸 수 없고, 데이터 도구도 직접 부를 수 없습니다.
  업뎃은 Dropbox·Overleaf 도구만, 일정은 캘린더 도구만 쓸 수 있습니다(PreToolUse 훅으로 강제).
- 업뎃·일정을 직접 부를 때(Slack 봇, `--agent`)는 내장 도구와 Agent 도구가 아예 없고, 자기 데이터 도구만 보이고
  쓸 수 있습니다. 다른 담당자의 도구를 부르려 해도 PreToolUse 훅이 막습니다.
- 사용자 설정 파일(`~/.claude/settings.json` 등)은 읽지 않아 도구 구성이 바뀌지 않습니다.
- 세 Slack 봇 모두 `SLACK_ALLOWED_USER_IDS`에 있는 사람의 메시지만 에이전트에게 넘기고, 이 값이 비어 있으면 시작하지 않습니다.
  봇 자신이나 우리 봇들끼리, 다른 봇의 메시지, 수정·입장 같은 시스템 메시지, 중복으로 들어온 이벤트는 무시합니다.
- Slack에 올리는 오류 메시지에는 오류 종류만 적습니다. 자세한 내용은 봇을 실행한 터미널(표준 오류)에만 남기고,
  Slack 토큰을 포함한 비밀값은 그 로그에서도 지웁니다.
- Slack에 올리는 답에서는 `@channel`·`@here` 같은 전체 알림을 막고, 링크 미리보기(unfurl)를 끕니다.

## 알려진 한계

- **Dropbox의 `modified_by`(마지막 수정자)는 공유 폴더 안의 파일에만 있습니다.**
  공유되지 않은 폴더의 파일은 누가 고쳤는지 알 수 없어 보고에서 빠집니다.
  공유 폴더인데도 수정자 정보가 없으면 "확인 불가"로 표시합니다.
- Dropbox는 **어떤 파일이 바뀌었는지만** 알려 주고, 무엇을 고쳤는지는 알려 주지 않습니다(토큰 절약).
  또 마지막 수정자 기준이라, 공저자가 고친 뒤 내가 다시 저장하면 마지막 수정자가 나라서 빠집니다.
- Overleaf도 **누가 어느 프로젝트를 언제 몇 번 편집했는지만** 알려 주고, 무엇을 고쳤는지는 알려 주지 않습니다(토큰 절약).
  편집 횟수는 Overleaf가 만든 git 커밋 수라서, 실제로 고친 횟수와 다를 수 있습니다.
- **Overleaf Git 연동은 유료 플랜 기능**이고, **커밋 작성자 정보는 Overleaf 히스토리에서 옵니다.**
  Overleaf가 여러 사람의 편집을 한 커밋으로 묶거나 계정 이름으로 표시할 수 있어서,
  `MY_NAMES`/`MY_EMAILS`를 Overleaf에 보이는 값과 맞춰야 내 커밋이 정확히 빠집니다.
- **알림은 터미널(표준 출력)과 Slack으로 받을 수 있습니다.** 이메일 전달은 아직 없습니다
  (cron 출력 파일을 메일로 보내는 식으로 붙일 수 있습니다).
- Slack 채널에서는 권한을 최소로 하려고 채널 메시지 읽기 권한을 받지 않습니다. 그래서 봇은 멘션한 메시지만 보고,
  스레드의 다른 메시지(다른 봇의 답 포함)는 읽지 않습니다. 대화도 봇마다 따로라서, 업뎃 봇에게 들은 내용을 고뭉치는 모릅니다.
- Dropbox·Overleaf의 "마지막 확인 시각"은 고뭉치, 업뎃 봇, `--agent update`가 함께 씁니다. 업뎃 봇으로 먼저 확인하면
  다음 고뭉치 브리핑에는 그 뒤의 변경만 나옵니다.
- 고뭉치에게 물으면 고뭉치·업뎃·일정이 모두 모델을 호출하므로 API 비용이 듭니다. 업뎃·일정을 직접 부르면 한 에이전트만
  호출합니다. Slack 멘션·DM도 한 번마다 비용이 듭니다.

## 개발

```bash
pip install -e '.[dev]'
pytest -q
```

테스트는 네트워크를 쓰지 않습니다. Dropbox 클라이언트와 git 실행은 가짜 객체로 대신하고,
캘린더는 테스트 안의 ICS 문자열과 고정된 시계로 확인합니다. Slack은 가짜 웹 클라이언트와 가짜 `run_turn`으로
확인하므로 실제 Slack이나 Claude에 연결하지 않습니다.

```
slack_manifests/       # Slack 앱 매니페스트, 봇마다 하나 (moongchi.yaml · update.yaml · schedule.yaml)
src/mungchi/
├── __main__.py        # python -m mungchi
├── main.py            # 페르소나별 ClaudeAgentOptions 구성, 한 턴 실행(run_turn), CLI(--agent 포함), 출력 스트리밍
├── personas.py        # 페르소나 키(mungchi·update·schedule), 한글 이름, Slack 핸들
├── slack_bot.py       # Slack 봇 세 개(한 프로세스, Socket Mode), 권한 확인, 진행 표시, --brief --slack
├── slack_format.py    # Slack용 프롬프트, 봇별 첫 답, 멘션 제거, mrkdwn 변환, 메시지 나누기
├── agents.py          # 고뭉치 프롬프트, 업뎃·일정 프롬프트(하위 에이전트용·직접 대화용), AgentDefinition, 도구 권한 훅
├── config.py          # 환경변수 읽기(봇별 Slack 토큰 포함), 설정 누락 안내 문구
├── state.py           # 마지막 확인 시각(.mungchi_state.json), 봇별 Slack 스레드↔대화(.mungchi_slack_threads.json)
└── tools/
    ├── __init__.py    # SDK MCP 서버(mungchi)와 도구 이름
    ├── common.py      # 비밀값 지우기, 인자 정리, 결과 JSON
    ├── dropbox_tool.py
    ├── overleaf_tool.py
    └── calendar_tool.py
```
