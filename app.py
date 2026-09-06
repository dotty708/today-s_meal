import streamlit as st
import requests
import pandas as pd
import plotly.express as px
from datetime import date, timedelta
import re

st.set_page_config(page_title="학교급식 비교 분석", layout="wide")

NEIS_MEAL_URL = "https://open.neis.go.kr/hub/mealServiceDietInfo"
NEIS_SCHOOL_URL = "https://open.neis.go.kr/hub/schoolInfo"

# 기본 비교 대상 학교 (정식 명칭 기준). 당곡고등학교를 기본 선택으로 둔다.
DEFAULT_SCHOOLS = ["당곡고등학교", "수도여자고등학교", "성남고등학교", "영락고등학교"]

# 사용자가 흔히 쓰는 축약 학교명도 검색어 후보로 사용 (검색 실패 시 대비)
SEARCH_ALIASES = {
    "당곡고등학교": ["당곡고등학교", "당곡고"],
    "수도여자고등학교": ["수도여자고등학교", "수도여고"],
    "성남고등학교": ["성남고등학교", "성남고"],
    "영락고등학교": ["영락고등학교", "영락고"],
}

# 디저트로 분류할 메뉴 키워드 (필요에 따라 자유롭게 추가/수정 가능)
DESSERT_KEYWORDS = [
    "빵", "케이크", "아이스크림", "요구르트", "요거트", "젤리", "푸딩",
    "쿠키", "도넛", "파이", "무스", "과일", "약과", "한과", "롤케익",
    "타르트", "요플레", "치즈스틱", "떡", "샌드위치빵",
]

WEEKDAY_KO = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]


# ------------------------- API 호출 함수 -------------------------

@st.cache_data(show_spinner=False, ttl=3600)
def search_school(api_key: str, school_name: str) -> pd.DataFrame:
    """학교명으로 NEIS 학교기본정보 API를 조회하여 후보 목록을 반환한다."""
    params = {
        "KEY": api_key,
        "Type": "json",
        "pIndex": 1,
        "pSize": 20,
        "SCHUL_NM": school_name,
    }
    try:
        res = requests.get(NEIS_SCHOOL_URL, params=params, timeout=10)
        res.raise_for_status()
        data = res.json()
    except Exception:
        return pd.DataFrame()

    if "schoolInfo" not in data or len(data["schoolInfo"]) < 2:
        return pd.DataFrame()

    rows = data["schoolInfo"][1]["row"]
    df = pd.DataFrame(rows)
    cols = ["ATPT_OFCDC_SC_CODE", "ATPT_OFCDC_SC_NM", "SD_SCHUL_CODE", "SCHUL_NM", "ORG_RDNMA"]
    cols = [c for c in cols if c in df.columns]
    return df[cols]


@st.cache_data(show_spinner=False, ttl=1800)
def fetch_meal_data(api_key: str, atpt_code: str, schul_code: str,
                     from_ymd: str, to_ymd: str, meal_code: str = "") -> pd.DataFrame:
    """급식식단정보 API를 호출하여(필요 시 페이지네이션 포함) 전체 데이터를 반환한다."""
    all_rows = []
    p_index = 1
    p_size = 100
    while True:
        params = {
            "KEY": api_key,
            "Type": "json",
            "pIndex": p_index,
            "pSize": p_size,
            "ATPT_OFCDC_SC_CODE": atpt_code,
            "SD_SCHUL_CODE": schul_code,
            "MLSV_FROM_YMD": from_ymd,
            "MLSV_TO_YMD": to_ymd,
        }
        if meal_code:
            params["MMEAL_SC_CODE"] = meal_code

        try:
            res = requests.get(NEIS_MEAL_URL, params=params, timeout=10)
            res.raise_for_status()
            data = res.json()
        except Exception:
            break

        if "mealServiceDietInfo" not in data or len(data["mealServiceDietInfo"]) < 2:
            break

        rows = data["mealServiceDietInfo"][1]["row"]
        all_rows.extend(rows)

        head = data["mealServiceDietInfo"][0]["head"]
        total_count = int(head[0]["list_total_count"])
        if p_index * p_size >= total_count:
            break
        p_index += 1

    if not all_rows:
        return pd.DataFrame()

    return pd.DataFrame(all_rows)


# ------------------------- 파싱 함수 -------------------------

def parse_dishes(ddish_nm: str):
    """요리명 문자열(<br/> 구분)을 파싱하여 (요리명, 알레르기번호) 리스트로 반환한다."""
    if not ddish_nm:
        return []
    items = ddish_nm.split("<br/>")
    parsed = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        m = re.match(r"^(.*?)\s*(?:\(([\d.\s]+)\))?$", item)
        name = m.group(1).strip() if m else item
        allergy = m.group(2).strip() if m and m.group(2) else ""
        parsed.append((name, allergy))
    return parsed


def parse_nutrition(ntr_info: str) -> dict:
    """영양정보 문자열(<br/> 구분, 'key : value' 형식)을 파싱하여 딕셔너리로 반환한다."""
    result = {}
    if not ntr_info:
        return result
    items = ntr_info.split("<br/>")
    for item in items:
        if ":" not in item:
            continue
        key, _, value = item.partition(":")
        key = key.strip()
        value = value.strip()
        try:
            result[key] = float(value)
        except ValueError:
            pass
    return result


def get_protein(ntr_dict: dict):
    """영양정보 딕셔너리에서 '단백질' 항목 값을 추출한다."""
    for k, v in ntr_dict.items():
        if "단백질" in k:
            return v
    return None


def has_dessert(dish_names) -> bool:
    return any(any(kw in name for kw in DESSERT_KEYWORDS) for name in dish_names)


def build_school_dataframe(api_key, school_label, atpt_code, schul_code,
                            from_ymd, to_ymd, meal_code) -> pd.DataFrame:
    raw = fetch_meal_data(api_key, atpt_code, schul_code, from_ymd, to_ymd, meal_code)
    if raw.empty:
        return pd.DataFrame()

    raw["MLSV_YMD"] = pd.to_datetime(raw["MLSV_YMD"], format="%Y%m%d")
    raw["요일"] = raw["MLSV_YMD"].dt.dayofweek.map(lambda i: WEEKDAY_KO[i])
    raw["학교명"] = school_label

    dish_lists = raw["DDISH_NM"].apply(lambda s: [n for n, _ in parse_dishes(s)])
    raw["요리목록"] = dish_lists
    raw["김치포함여부"] = dish_lists.apply(lambda names: any("김치" in n for n in names))
    raw["김치개수"] = dish_lists.apply(lambda names: sum(1 for n in names if "김치" in n))
    raw["디저트포함여부"] = dish_lists.apply(has_dessert)

    ntr_dicts = raw["NTR_INFO"].apply(parse_nutrition)
    raw["단백질(g)"] = ntr_dicts.apply(get_protein)

    keep_cols = ["학교명", "MLSV_YMD", "요일", "MMEAL_SC_NM", "요리목록",
                 "김치포함여부", "김치개수", "디저트포함여부", "단백질(g)", "CAL_INFO"]
    keep_cols = [c for c in keep_cols if c in raw.columns]
    return raw[keep_cols].sort_values("MLSV_YMD").reset_index(drop=True)


# ------------------------- UI -------------------------

st.title("🍱 학교급식 비교 분석 대시보드")
st.caption("NEIS 나이스 급식식단정보 Open API 기반 · Plotly 시각화")

with st.sidebar:
    st.header("⚙️ 설정")
    api_key = st.text_input(
        "NEIS API 인증키", type="password",
        help="https://open.neis.go.kr 에서 발급. 미입력 시 학교당 5건만 샘플 조회됩니다.",
    )
    if not api_key:
        st.warning("인증키 미입력: 샘플키로 동작하며 결과가 5건으로 제한됩니다.")
        api_key = "sample"

    st.divider()
    st.subheader("📅 분석 기간")
    today = date.today()
    default_start = today - timedelta(days=30)
    start_date = st.date_input("시작일", value=default_start)
    end_date = st.date_input("종료일", value=today)

    st.divider()
    st.subheader("🍽️ 식사 구분")
    meal_option = st.selectbox("식사코드", ["전체", "조식", "중식", "석식"], index=2)
    meal_code_map = {"전체": "", "조식": "1", "중식": "2", "석식": "3"}
    meal_code = meal_code_map[meal_option]

    st.divider()
    st.subheader("🏫 학교 선택")
    selected_school_names = st.multiselect(
        "비교할 학교를 선택하세요 (당곡고등학교 기본 선택)",
        options=DEFAULT_SCHOOLS,
        default=["당곡고등학교"],
    )

if start_date > end_date:
    st.error("시작일은 종료일보다 빠를 수 없습니다.")
    st.stop()

if not selected_school_names:
    st.info("왼쪽 사이드바에서 최소 1개 이상의 학교를 선택해주세요.")
    st.stop()

from_ymd = start_date.strftime("%Y%m%d")
to_ymd = end_date.strftime("%Y%m%d")

# ---- 학교 코드 확인 (검색 + 동명 학교 후보 선택) ----
school_codes = {}
with st.spinner("학교 정보를 조회하는 중..."):
    for school in selected_school_names:
        cache_key = f"school_code::{school}"
        if cache_key in st.session_state:
            school_codes[school] = st.session_state[cache_key]
            continue

        candidates = pd.DataFrame()
        for alias in SEARCH_ALIASES.get(school, [school]):
            candidates = search_school(api_key, alias)
            if not candidates.empty:
                break

        if candidates.empty:
            st.error(f"'{school}' 학교 정보를 찾을 수 없습니다. 인증키 또는 학교명을 확인하세요.")
            continue

        if len(candidates) == 1:
            row = candidates.iloc[0]
        else:
            st.write(f"**'{school}'** 검색 결과가 여러 개입니다. 정확한 학교를 선택하세요:")
            labels = [f"{r['SCHUL_NM']} ({r.get('ORG_RDNMA', '')})" for _, r in candidates.iterrows()]
            idx = st.radio(f"{school} 후보 선택", options=list(range(len(labels))),
                            format_func=lambda i: labels[i], key=f"radio_{school}")
            row = candidates.iloc[idx]

        code_info = (row["ATPT_OFCDC_SC_CODE"], row["SD_SCHUL_CODE"])
        st.session_state[cache_key] = code_info
        school_codes[school] = code_info

if not school_codes:
    st.stop()

# ---- 급식 데이터 수집 ----
all_frames = []
with st.spinner("급식 데이터를 불러오는 중..."):
    for school, (atpt_code, schul_code) in school_codes.items():
        df = build_school_dataframe(api_key, school, atpt_code, schul_code, from_ymd, to_ymd, meal_code)
        if df.empty:
            st.warning(f"'{school}'의 해당 기간 급식 데이터가 없습니다.")
        else:
            all_frames.append(df)

if not all_frames:
    st.stop()

meal_df = pd.concat(all_frames, ignore_index=True)

st.success(f"총 {len(meal_df)}건의 급식 데이터를 불러왔습니다. (기간: {start_date} ~ {end_date})")

tab1, tab2, tab3, tab4 = st.tabs(
    ["🥬 김치 등장 분석", "🥩 평균 단백질 분석", "🍰 디저트 요일 분석", "📋 원본 데이터"]
)

# ---------------- 탭 1: 김치 분석 ----------------
with tab1:
    st.subheader("기간 내 '김치' 급식 등장 횟수")

    kimchi_summary = meal_df.groupby("학교명").agg(
        총_급식일수=("MLSV_YMD", "nunique"),
        김치_등장일수=("김치포함여부", "sum"),
        김치_총개수=("김치개수", "sum"),
    ).reset_index()
    kimchi_summary["등장비율(%)"] = (
        kimchi_summary["김치_등장일수"] / kimchi_summary["총_급식일수"] * 100
    ).round(1)

    col1, col2 = st.columns(2)
    with col1:
        fig_kimchi = px.bar(
            kimchi_summary, x="학교명", y="김치_등장일수",
            text="김치_등장일수", color="학교명",
            title="학교별 김치가 등장한 급식일수",
            labels={"김치_등장일수": "김치 등장 일수"},
        )
        fig_kimchi.update_traces(textposition="outside")
        st.plotly_chart(fig_kimchi, use_container_width=True)
    with col2:
        fig_ratio = px.bar(
            kimchi_summary, x="학교명", y="등장비율(%)",
            text="등장비율(%)", color="학교명",
            title="학교별 김치 등장 비율(전체 급식일 대비)",
        )
        fig_ratio.update_traces(textposition="outside")
        st.plotly_chart(fig_ratio, use_container_width=True)

    st.dataframe(kimchi_summary, use_container_width=True)

    timeline = meal_df[meal_df["김치포함여부"]]
    if not timeline.empty:
        fig_timeline = px.scatter(
            timeline, x="MLSV_YMD", y="학교명", color="학교명",
            title="김치가 나온 날짜 타임라인",
            labels={"MLSV_YMD": "날짜"},
        )
        st.plotly_chart(fig_timeline, use_container_width=True)

# ---------------- 탭 2: 단백질 분석 ----------------
with tab2:
    st.subheader("학교별 평균 단백질 함유량 (g)")

    protein_df = meal_df.dropna(subset=["단백질(g)"])
    if protein_df.empty:
        st.info("단백질 정보가 포함된 데이터가 없습니다.")
    else:
        protein_summary = protein_df.groupby("학교명")["단백질(g)"].agg(
            평균_단백질="mean", 최대_단백질="max", 최소_단백질="min"
        ).round(1).reset_index()

        fig_protein = px.bar(
            protein_summary, x="학교명", y="평균_단백질",
            text="평균_단백질", color="학교명",
            title="학교별 평균 단백질 함유량(g)",
            labels={"평균_단백질": "평균 단백질(g)"},
        )
        fig_protein.update_traces(textposition="outside")
        st.plotly_chart(fig_protein, use_container_width=True)

        fig_protein_trend = px.line(
            protein_df.sort_values("MLSV_YMD"), x="MLSV_YMD", y="단백질(g)",
            color="학교명", markers=True,
            title="날짜별 단백질 함유량 추이",
        )
        st.plotly_chart(fig_protein_trend, use_container_width=True)

        st.dataframe(protein_summary, use_container_width=True)

# ---------------- 탭 3: 디저트 요일 분석 ----------------
with tab3:
    st.subheader("디저트가 가장 자주 나오는 요일")
    st.caption(
        "디저트 판별 키워드: " + ", ".join(DESSERT_KEYWORDS) +
        " (사이드바 코드 내 DESSERT_KEYWORDS 리스트에서 자유롭게 수정 가능)"
    )

    dessert_df = meal_df[meal_df["디저트포함여부"]]
    if dessert_df.empty:
        st.info("해당 기간 동안 디저트로 분류될 만한 메뉴가 없습니다.")
    else:
        weekday_order = WEEKDAY_KO
        dessert_counts = (
            dessert_df.groupby(["학교명", "요일"]).size()
            .reset_index(name="디저트_등장횟수")
        )
        dessert_counts["요일"] = pd.Categorical(
            dessert_counts["요일"], categories=weekday_order, ordered=True
        )
        dessert_counts = dessert_counts.sort_values(["학교명", "요일"])

        fig_dessert = px.bar(
            dessert_counts, x="요일", y="디저트_등장횟수", color="학교명",
            barmode="group", title="요일별 디저트 등장 횟수",
            category_orders={"요일": weekday_order},
        )
        st.plotly_chart(fig_dessert, use_container_width=True)

        st.markdown("**학교별 디저트가 가장 자주 나오는 요일**")
        top_day = (
            dessert_counts.sort_values("디저트_등장횟수", ascending=False)
            .groupby("학교명").first().reset_index()
        )
        for _, row in top_day.iterrows():
            st.write(f"- **{row['학교명']}**: {row['요일']} ({row['디저트_등장횟수']}회)")

        st.dataframe(dessert_counts, use_container_width=True)

# ---------------- 탭 4: 원본 데이터 ----------------
with tab4:
    st.subheader("원본 급식 데이터")
    display_df = meal_df.copy()
    display_df["요리목록"] = display_df["요리목록"].apply(lambda x: ", ".join(x))
    st.dataframe(display_df, use_container_width=True)

    csv = display_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button("CSV로 다운로드", data=csv, file_name="meal_data.csv", mime="text/csv")
