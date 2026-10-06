"""Record link-discovery hints and actual browser pagination evidence."""

import asyncio
import json
import re
from pathlib import Path

import httpx
from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]


async def main():
    output = ROOT / 'exports' / 'list_discovery'
    output.mkdir(parents=True, exist_ok=True)
    observed = []
    tasks = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel='chrome', headless=True)
        try:
            page = await browser.new_page()

            async def capture(response):
                if '/api/hr163/position/queryPage' not in response.url:
                    return
                data = await response.json()
                observed.append({
                    'url': response.url, 'method': response.request.method,
                    'body': json.loads(response.request.post_data or '{}'),
                    'http_status': response.status, 'success_code': data.get('code'),
                    'total': data.get('data', {}).get('total'),
                    'pages': data.get('data', {}).get('pages'),
                    'ids': [str(item['id']) for item in data.get('data', {}).get('list', [])],
                })

            def on_response(response):
                task = asyncio.create_task(capture(response))
                tasks.append(task)

            page.on('response', on_response)
            await page.goto('https://hr.163.com/job-list.html',
                            wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(2500)
            html = await page.content()
            (output / 'netease_page.html').write_text(html, encoding='utf-8')
            links = await page.locator('a[href]').evaluate_all(
                '(nodes) => nodes.map(n => ({text:n.innerText,url:n.href}))')
            scripts = await page.locator('script[src]').evaluate_all(
                '(nodes) => nodes.map(n => n.src)')
            controls = await page.locator('li.ant-pagination-item').all_text_contents()
            await page.locator('li.ant-pagination-next').click(timeout=10000)
            await page.wait_for_timeout(2000)
            await asyncio.gather(*tasks)
            active = await page.locator('li.ant-pagination-item-active').inner_text()
        finally:
            await browser.close()

    # Regex scans source strings for hints, not for proof of supported endpoints.
    patterns = {
        'recruitment_link': r'job|career|position|campus|recruit',
        'endpoint_literal': r'[\"\x27]([^\"\x27\s]{0,180}(?:queryPage|searchPosition|/api/)[^\"\x27\s]{0,180})[\"\x27]',
        'page_parameter': r'\b(?:currentPage|pageIndex|pageSize|totalPages|total|pages)\b',
    }
    source_hints = []
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        for url in scripts[:20]:
            if not url.startswith('https://hr.163.com/'):
                continue
            try:
                response = client.get(url)
                response.raise_for_status()
                if len(response.content) > 5 * 1024 * 1024:
                    continue
                text = response.text
                source_hints.append({
                    'script_url': url,
                    'endpoint_candidates': list(dict.fromkeys(re.findall(patterns['endpoint_literal'], text)))[:80],
                    'pagination_tokens': sorted(set(re.findall(patterns['page_parameter'], text))),
                })
            except httpx.HTTPError as exc:
                source_hints.append({'script_url': url, 'error_type': type(exc).__name__})
    report = {
        'source_url': 'https://hr.163.com/job-list.html',
        'candidate_links': [link for link in links if re.search(patterns['recruitment_link'], link['url'], re.I)],
        'regex_patterns': patterns, 'script_hints': source_hints,
        'visible_page_numbers_before': controls,
        'active_page_after_click': active, 'observed_requests': observed,
        'actual_pagination_verified': len(observed) >= 2 and
            observed[0]['body'].get('currentPage') == 1 and
            observed[-1]['body'].get('currentPage') == 2 and
            observed[0]['ids'] != observed[-1]['ids'],
        'regex_proves_completeness': False,
    }
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'actual_pagination_verified': report['actual_pagination_verified'],
                      'observed_pages': [r['body'].get('currentPage') for r in observed],
                      'report': str(output / 'report.json')}, ensure_ascii=True))


if __name__ == '__main__':
    asyncio.run(main())
