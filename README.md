# 스타일씨 체험단 자동 신청

매일 **한국시간 오전 10시**에 GitHub가 스타일씨 제공형 체험단을 자동으로 신청하고,
결과를 대시보드 페이지에 보여줍니다.

**대시보드:** https://hdongmi.github.io/stylec-auto/

- 오늘 신청한 개수, 다음 자동 신청까지 남은 시간
- 최근 2주 일별 신청 그래프
- 신청 내역 (완료 / 확인 필요 / 실패 필터, 누르면 체험단 페이지로 이동)
- 자동 실행 기록, 로그인 만료 같은 문제가 생기면 맨 위에 안내

이미 신청한 체험단은 다시 신청하지 않습니다. 마감·유형 다름 등으로 건너뛴 건은 기록하지 않습니다.

## 구성

| 파일 | 역할 |
|---|---|
| `stylec_auto.py` | 신청 프로그램 (내 컴퓨터에서도 그대로 사용 가능) |
| `.github/workflows/stylec.yml` | 매일 10시 실행 → 기록 커밋 → 대시보드 배포 |
| `.github/workflows/pages.yml` | 대시보드 파일이 바뀌면 다시 배포 |
| `docs/index.html` | 대시보드 페이지 |
| `docs/data/history.json` | 신청·실행 기록 (자동으로 쌓임) |

---

## 처음 한 번만 하는 설정

### 1. 로그인 상태 내보내기 (내 컴퓨터)

카카오 로그인은 GitHub 서버에서 직접 할 수 없어서, 내 컴퓨터의 로그인 상태를 넘겨줍니다.
이 저장소의 `stylec_auto.py`로 내 컴퓨터 `stlyc` 폴더의 파일을 바꾼 뒤:

```
cd C:\Users\user\Desktop\stlyc
python stylec_auto.py --export-session
```

폴더에 `session_b64.txt`가 생깁니다. **계정 열쇠와 같으니 다른 사람에게 보내거나 저장소에 올리지 마세요.**

### 2. 시크릿 등록

저장소 **Settings → Secrets and variables → Actions → New repository secret**

| Name | Secret |
|---|---|
| `STYLEC_SESSION` | `session_b64.txt` 내용 전체 |

### 3. 대시보드 켜기

저장소 **Settings → Pages → Build and deployment → Source** 를 **GitHub Actions** 로 바꿉니다.

### 4. 시험 실행

**Actions** 탭 → **체험단 자동 신청** → **Run workflow**.
끝나면 대시보드 주소에서 결과를 볼 수 있습니다. 이후로는 매일 오전 10시에 자동 실행됩니다.

---

## 관리

- **로그인 만료**: 대시보드 맨 위에 빨간 안내가 뜨고 GitHub에서 실패 메일이 옵니다.
  내 컴퓨터에서 (필요하면 `python stylec_auto.py --login` 후) `--export-session` 을 다시 실행하고
  `STYLEC_SESSION` 시크릿을 새 값으로 **Update** 하세요.
- **신청 문구·제외 키워드**: `.github/workflows/stylec.yml` 의 `APPLY_MESSAGE`, `EXCLUDE`.
- **실행 시각**: `stylec.yml` 의 `cron`(UTC 기준, 한국시간 −9시간)과 `docs/index.html` 의 `RUN_HOUR_KST` 를 함께 바꾸세요.
- **직접 신청이 필요한 건**: Actions 실행 결과 하단 **Artifacts → screenshots** 에 화면이 저장됩니다.

## 알아둘 점

- GitHub 예약 실행은 붐비는 시간에 **10~30분 늦게** 시작될 수 있습니다.
- 공개 저장소에 **60일 동안 커밋이 없으면** 예약 실행이 꺼지는데, 매일 기록이 커밋되므로 보통 문제없습니다.
- 공개 저장소라 코드와 신청 기록(체험단 이름)은 누구나 볼 수 있습니다. 로그인 정보는 시크릿에만 있어 공개되지 않습니다.
- GitHub 서버는 해외 IP라 사이트가 접속을 막거나 로그인을 풀 수 있습니다.
- 자동 신청은 사이트 이용약관에 따라 계정 제재 대상이 될 수 있고, 선정되면 리뷰 작성 의무가 생깁니다.

## 내 컴퓨터에서 실행

```
python stylec_auto.py --login                 # 최초 1회 로그인
python stylec_auto.py                         # 미리보기 (신청 안 함)
python stylec_auto.py --apply --max 10        # 실제 신청
python stylec_auto.py --apply --history docs/data/history.json   # 대시보드 기록까지
```
