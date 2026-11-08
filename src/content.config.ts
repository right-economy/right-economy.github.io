import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';
import { CATEGORIES } from './consts';

// 경제 글 카드 컬렉션. src/content/posts/*.md 를 자동으로 읽어온다.
const posts = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/posts' }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    // 자유 문자열이 아니라 src/consts.ts 의 CATEGORIES 로 가둔다. 카테고리 허브는
    // 원고에 적힌 값으로 그냥 생겨나서(src/pages/category/[name].astro), 오타나
    // 임의 작명이 들어오면 글 몇 편만 묶인 허브가 새로 서고 sitemap 에 올라간다.
    // 여기서 막으면 발행 전 빌드에서 걸린다.
    category: z.enum(CATEGORIES),
    publishedDate: z.coerce.date(),
    tags: z.array(z.string()).default([]),
  }),
});

export const collections = { posts };
