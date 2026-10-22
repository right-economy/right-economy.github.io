// 구조화 데이터(JSON-LD)에서 여러 페이지가 공유하는 노드.
//
// 왜 한곳에 모으나 — Organization 은 `@id`(#organization) 하나로 사이트 전체를
// 가리키는 노드다. 그런데 페이지마다 리터럴을 복사해 두면 같은 `@id` 에 서로 다른
// 속성이 실린다. 2026-10-19 실측: 301장은 logo 가 favicon-cat.png(256×256) +
// sameAs 가 붙은 모양이었고, /about/ 1장만 logo 가 mascot/logo.png(135×172)에
// parentOrganization 이 붙은 다른 모양이었다. 같은 엔티티를 두 가지로 설명한 것이고,
// 그 135×172 는 「정사각 권장·최소 112px」이라는 구글 로고 가이드에도 어긋났다.
//
// 또 하나 — /contact/ · /privacy/ · /disclaimer/ 세 장은 publisher 로 이 `@id` 를
// 참조하면서 정작 노드를 자기 @graph 안에 두지 않았다. `@id` 참조는 같은 페이지의
// JSON-LD 안에서만 풀리므로, 저 세 장에서는 발행 주체가 빈 참조로 남아 있었다.
import { SITE_NAME, SITE_DESCRIPTION, SITE_INSTAGRAM } from '../consts';
import { withBase } from './url';

/** `@id` 로 참조만 할 때 쓴다. 노드 본체는 organizationNode() 가 만든다. */
export function orgId(site: URL | undefined): string {
  return `${new URL(withBase('/'), site).href}#organization`;
}

/**
 * 사이트를 대표하는 발행 주체. sameAs 로 공식 인스타와 같은 주체임을 밝히고,
 * parentOrganization 으로 모회사를 적는다(E-E-A-T 신호).
 *
 * logo 는 정사각 실로고를 써야 검색 리치결과에 브랜드 로고로 인식된다
 * (구글 가이드: 정사각 권장·최소 112px). og-default(1200×630 배너)는 공유
 * 미리보기용이라 로고로는 부적합해서 256×256 마스코트 favicon 을 쓴다.
 */
export function organizationNode(site: URL | undefined) {
  const siteUrl = new URL(withBase('/'), site).href;
  return {
    '@type': 'Organization',
    '@id': `${siteUrl}#organization`,
    name: SITE_NAME,
    url: siteUrl,
    description: SITE_DESCRIPTION,
    logo: {
      '@type': 'ImageObject',
      url: new URL(withBase('/favicon-cat.png'), site).href,
    },
    sameAs: [SITE_INSTAGRAM],
    parentOrganization: { '@type': 'Organization', name: '글린트프로젝트' },
  };
}
