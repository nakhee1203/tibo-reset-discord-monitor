# Tibo GPT/Codex Reset → Discord Monitor (완전 무료 버전)

`@thsottiaux`의 공개 X 게시물을 **유료 X API 없이** 확인하고, GPT/Codex usage limit **리셋 예고**로 판단되면 Discord Webhook으로 알림을 보내는 모니터입니다.

## 비용

이 프로젝트 자체는 **0원 구성**입니다.

- X 공식 API: **사용 안 함**
- OpenAI API: **사용 안 함**
- Discord Webhook: 무료
- Python 패키지: 외부 패키지 없음
- 실행: **Public GitHub 저장소 + 표준 GitHub Actions runner = 무료**

> **5분 감시를 완전히 0원으로 유지하려면 Public 저장소를 권장합니다.** GitHub Free의 Private 저장소는 월 2,000분의 Actions 시간이 포함되지만, 각 job의 부분 사용 시간도 분 단위로 올림되므로 5분 cron은 무료 한도를 넘길 수 있습니다. Private 저장소를 꼭 써야 한다면 아래의 **30분 모드**를 사용하세요.

> 주의: 무료 공개 소스는 X 공식 API보다 안정성이 낮습니다. FxTwitter 같은 공개 서비스가 중단/차단되면 일시적으로 감지가 늦거나 실패할 수 있습니다. 그래서 이 프로젝트는 **FxTwitter RSS + Codex Radar 공개 JSON**을 함께 확인합니다.

## 동작 구조

```text
@thsottiaux 새 게시물
        ↓
FxTwitter RSS (무료)
        +
Codex Radar 공개 JSON (무료 보조 소스)
        ↓
게시물 ID로 중복 제거
        ↓
reset / usage limits / tomorrow / incoming 등의 문맥 판정
        ↓
Discord Webhook
        ↓
휴대폰 + PC Discord 푸시 알림
```

기본적으로 GitHub Actions가 5분마다 실행됩니다. 예약 실행은 서버 혼잡에 따라 정확히 5분마다 시작되지 않고 늦어질 수 있습니다.

## 1. Discord Webhook 만들기

1. 알림을 받을 Discord 서버의 텍스트 채널로 이동
2. **채널 설정 → 연동(Integrations) → Webhooks**
3. **New Webhook / 새 웹후크** 생성
4. 알림 받을 채널 선택
5. **Copy Webhook URL**

Webhook URL은 비밀번호처럼 취급하세요. GitHub 코드에 직접 넣지 않습니다.

### 내 계정을 강제로 멘션해서 푸시 받기

1. Discord 설정 → 고급 → **개발자 모드** 켜기
2. 자신의 프로필 우클릭/길게 누르기 → **사용자 ID 복사**
3. ID가 `123456789012345678`이라면 아래 Secret 값은:

```text
<@123456789012345678>
```

역할 멘션은 `<@&ROLE_ID>`, 서버 전체는 `@everyone`도 가능합니다.

## 2. GitHub 저장소 만들기

이 폴더의 파일을 새 GitHub 저장소에 업로드합니다.

**완전 무료 + 5분 감시를 원하면 Public 저장소로 만드세요.** 소스 코드와 `state.json`만 공개되고, GitHub Secret에 넣은 Discord Webhook URL은 저장소 파일에 노출되지 않습니다. **Webhook URL은 절대 파일에 넣지 말고 Secret으로만 보관하세요.**

저장소에서:

**Settings → Secrets and variables → Actions → New repository secret**

필수 Secret:

| 이름 | 값 |
|---|---|
| `DISCORD_WEBHOOK_URL` | Discord에서 복사한 Webhook URL |

권장 Secret:

| 이름 | 값 예시 |
|---|---|
| `DISCORD_MENTION` | `<@123456789012345678>` |

**X API 토큰은 필요 없습니다.**

### Private 저장소를 꼭 쓰고 싶다면

GitHub Free Private 저장소는 월 2,000 Actions 분이 포함됩니다. GitHub는 job의 부분 사용 시간도 다음 1분으로 올림합니다. 따라서 5분마다 하루 종일 실행하면 무료 한도를 넘길 수 있습니다.

`.github/workflows/monitor.yml`에서:

```yaml
- cron: "*/5 * * * *"
```

를 다음처럼 바꾸면 월 최대 약 1,440회 실행이므로, 각 실행이 1분 이내인 일반적인 경우 무료 한도 안에 들어옵니다.

```yaml
- cron: "*/30 * * * *"
```

다만 이 경우 알림은 최대 약 30분 + GitHub 스케줄 지연만큼 늦을 수 있습니다. 다른 GitHub Actions도 같은 계정에서 많이 사용한다면 남은 무료 시간에 영향을 받을 수 있습니다.

## 3. 첫 실행

GitHub 저장소에서:

**Actions → Tibo Reset Monitor (Free) → Run workflow**

첫 실행은 현재 가장 최신 게시물을 기준점으로 저장하고 Discord에 **무료 감시 시작** 메시지를 보냅니다.

기존 과거 게시물 때문에 갑자기 알람이 울리는 것을 막기 위해 첫 실행에서는 과거 reset 글을 알림하지 않습니다.

그 다음 실행부터 새 게시물만 판정합니다.

## 4. 어떤 글에 알람이 오나

### 강한 리셋 예고 → 항상 알림

예:

- `I will reset usage limits this evening`
- `reset incoming`
- `reset will land in the next hour`
- `Resetting the limits tomorrow morning`
- `Full reset for Codex users tomorrow`

Discord 제목:

```text
🚨 GPT/Codex 리셋 예고 감지
```

### 애매한 관련 글 → 기본적으로 알림

예:

```text
Looking into a Codex usage limit reset
```

오탐이 거슬리면 `.github/workflows/monitor.yml`의:

```yaml
ALERT_AMBIGUOUS: "true"
```

를 `false`로 바꾸세요.

### 이미 리셋 완료된 글 → 기본적으로 알림 안 함

예:

- `Usage limits have been reset`
- `The reset has been propagated`

완료 글도 받고 싶다면:

```yaml
ALERT_COMPLETED: "true"
```

로 변경합니다.

## 5. 무료 데이터 소스

기본값:

```text
https://fxtwitter.com/thsottiaux/feed.xml
https://codexradar.com/current.json
https://codex-reset-radar.pages.dev/current.json
```

FxTwitter RSS를 우선 읽고 Codex Radar 공개 JSON에서 Tibo의 X 링크가 포함된 신호도 같이 확인합니다.

무료 소스 주소를 나중에 바꾸고 싶다면 환경 변수로 재정의할 수 있습니다.

```text
FREE_FEED_URLS=https://example.com/{handle}/feed.xml
RADAR_URLS=https://example.com/current.json,https://backup.example.com/current.json
```

## 6. 로컬 테스트

Python 3.11+ 권장. `pip install`은 필요 없습니다.

macOS/Linux:

```bash
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
export DISCORD_MENTION='<@YOUR_USER_ID>'
python monitor.py
```

PowerShell:

```powershell
$env:DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
$env:DISCORD_MENTION='<@YOUR_USER_ID>'
python monitor.py
```

분류기와 RSS/JSON 파서를 테스트하려면:

```bash
python -m unittest -v test_monitor.py
```

## 7. 파일 설명

```text
monitor.py                     실제 감시 + 판정 + Discord 알림
state.json                     마지막으로 처리한 X 게시물 ID
.github/workflows/monitor.yml  5분마다 실행하는 GitHub Actions
.env.example                   로컬 실행 설정 예시
test_monitor.py                자동 테스트
```

`state.json`은 GitHub Actions가 새 글을 처리할 때 자동 커밋합니다.

## 8. 중요한 한계

**돈은 들지 않지만 X 공식 API 방식만큼 보장되지는 않습니다.**

무료 감시는 FxTwitter/Codex Radar 등 제3자 공개 서비스에 의존합니다. X가 접근 방식을 변경하거나 무료 소스가 장애를 겪으면 해당 시간 동안 글을 못 볼 수 있습니다. 프로그램은 다음 실행에서 다시 시도하며, 사용할 수 있는 게시물을 찾지 못한 경우 `state.json`을 앞으로 넘기지 않아서 복구 뒤 새 글을 다시 잡을 수 있도록 했습니다.

또한 GitHub Actions cron은 정확한 실시간 시스템이 아니어서 알림이 몇 분 늦을 수 있습니다.

## 보안

- Discord Webhook URL을 GitHub 코드/README/state.json에 직접 넣지 마세요.
- 반드시 GitHub Actions **Repository Secret**으로 저장하세요.
- Webhook URL이 유출되었다면 Discord에서 기존 Webhook을 삭제하고 새로 만드세요.
