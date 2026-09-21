/**
 * 显示层小工具的测试 —— `npm run test`。
 *
 * 这一条守的是**只读代码审查 JH-10**：快照里的 URL 是外部输入，
 * 只有 `http` / `https` 才允许被渲染成页面上可点的 `href`。
 * `file:///C:/secret.txt`、`javascript:…`、`data:…` 必须退化成纯文本。
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { httpUrlOrNull, orDash } from './format.ts';

test('http / https 原样返回（正常链接照旧可点）', () => {
  assert.equal(httpUrlOrNull('https://example.com/careers/1'), 'https://example.com/careers/1');
  assert.equal(httpUrlOrNull('http://example.com/'), 'http://example.com/');
  // 大小写不敏感（用 URL 解析器判断，不靠字符串前缀匹配）
  assert.equal(httpUrlOrNull('HTTPS://example.com/x'), 'HTTPS://example.com/x');
  // 首尾空白先去掉
  assert.equal(httpUrlOrNull('  https://example.com/x  '), 'https://example.com/x');
});

test('★ 非 http(s) 一律返回 null（页面按纯文本显示，不给 href）', () => {
  const dangerous = [
    'file:///C:/secret.txt',
    'javascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'ftp://example.com/f',
    '//example.com/protocol-relative',
    'example.com/没有协议',
  ];
  for (const value of dangerous) {
    assert.equal(httpUrlOrNull(value), null, `不该被当成可点链接：${value}`);
  }
});

test('空值返回 null（页面显示「—」而不是空链接）', () => {
  assert.equal(httpUrlOrNull(null), null);
  assert.equal(httpUrlOrNull(undefined), null);
  assert.equal(httpUrlOrNull(''), null);
  assert.equal(httpUrlOrNull('   '), null);
});

test('orDash 仍然把空值显示成「—」', () => {
  assert.equal(orDash(null), '—');
  assert.equal(orDash('  '), '—');
  assert.equal(orDash('内推'), '内推');
});
