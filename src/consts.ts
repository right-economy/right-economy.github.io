// 사이트 전역 상수 (SEO·수익화 공용).
export const SITE_NAME = '옳은경제';
export const SITE_TAGLINE = '복잡한 경제, 바른 방향을 짚어주는';
export const SITE_DESCRIPTION =
  '금리·물가·환율부터 투자 기초까지. 어려운 경제를 쉽고 정확하게 정리하는 사이트.';
export const SITE_LOCALE = 'ko_KR';

// 공식 소셜 계정. 구조화 데이터(Organization sameAs)에 넣어 사이트와 SNS를
// 같은 주체로 연결한다 — 검색엔진 엔티티 인식·신뢰(E-E-A-T) 신호.
export const SITE_INSTAGRAM = 'https://www.instagram.com/right.economy/';

// Google AdSense 게시자 ID. 애드센스 승인 후 'ca-pub-XXXXXXXXXXXXXXXX' 형태로 채우면
// 자동으로 <head>에 게시자 스크립트가 삽입된다. 비워두면 광고 스크립트 미삽입.
export const ADSENSE_CLIENT = 'ca-pub-5346278223838545';

// Google Search Console 소유확인 메타 토큰. 발급받은 content 값만 넣으면 됨. 비우면 미삽입.
export const GSC_VERIFICATION = 'URWVEa07_MRqW1giNH0NA8CVjRagzYNQ23BcWcmvYS0';

// 카테고리 허브의 meta description. 원래는 카테고리명만 끼운 한 문장을 9개 허브가
// 공유했는데(「옳은경제의 <카테고리> 분야 글을 모아봤습니다. …」), 검색결과에
// 나란히 뜨면 설명이 사실상 같아 검색엔진이 그 설명을 버리고 본문에서 임의로
// 뽑아 쓴다. 허브마다 그 분야에서 실제로 다루는 것을 적는다.
// 여기에 없는 카테고리는 아래 CATEGORY_DESCRIPTION_FALLBACK 으로 떨어진다.
// (2026-10-22 SEO 감사 — 9개 허브 설명이 45~48자에 카테고리명만 달랐다)
export const CATEGORY_DESCRIPTIONS: Record<string, string> = {
  '부동산': '서울·수도권 실거래와 분양가, 입주 물량, 경매 낙찰가율, 종부세와 중개보수까지. 집을 사고팔 때 돈이 걸리는 숫자를 직접 세어 확인합니다.',
  '기초 개념': '금리·채권·배당기준일·통화스와프처럼 뉴스에 그냥 나오는 말을 처음부터 풀어씁니다. 용어가 잡히면 그다음 기사가 읽힙니다.',
  '시황': '그날 코스피·코스닥 종가와 투자자별 매매, 환율·미국 증시·유가를 수치로 정리합니다. 직전 시황에 적어둔 관전 포인트가 어떻게 됐는지도 이어서 봅니다.',
  '생활 경제': '예금과 비과세, 카드 공제와 연말정산, 코픽스 대출이자, 퇴직연금, 증여 기준까지. 내가 해당되는 조건인지부터 금액으로 가릅니다.',
  '지표 읽기': '근원물가·소비자심리지수·PMI·점도표·한미 금리차를 어디서 보고 무엇과 같이 봐야 하는지 정리합니다. 숫자 하나로 결론 내지 않는 법까지.',
  '투자': '주식·ETF·ELS·레버리지의 구조와 세금, 반대매매나 관리종목처럼 손실이 확정되는 자리를 설명합니다. 특정 종목은 권하지 않습니다.',
  '경제 뉴스': '금통위 금리 결정과 통계 발표, 국채금리·금값이 움직인 날을 원문 수치와 함께 짚습니다. 왜 그렇게 됐는지까지 적습니다.',
};

export const CATEGORY_DESCRIPTION_FALLBACK = (category: string) =>
  `${SITE_NAME}의 ${category} 분야 글 목록입니다. 어려운 경제를 쉽고 정확하게 정리합니다.`;
