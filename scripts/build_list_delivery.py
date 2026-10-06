"""Build a small, provenance-preserving view of already collected lists."""

import csv
import json
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    {'name': 'tencent_campus', 'company': 'Tencent', 'post_key': 'postId',
     'url_template': 'https://join.qq.com/post_detail.html?postid={id}'},
    {'name': 'netease_campus_internet', 'company': 'NetEase', 'post_key': 'id',
     'identity_namespace': 'NetEase-campus-103',
     'url_template': 'https://campus.163.com/app/detail/index?id={id}&projectId=103'},
    {'name': 'netease_campus_games', 'company': 'NetEase', 'post_key': 'id',
     'identity_namespace': 'NetEase-campus-102',
     'url_template': 'https://campus.game.163.com/app/detail/index?id={id}&projectId=102'},
    {'name': 'netease_campus_leihuo', 'company': 'NetEase', 'post_key': 'ehr_job_id',
     'identity_namespace': 'NetEase-leihuo-77'},
    {'name': 'netease_campus_ai_intern', 'company': 'NetEase', 'post_key': 'id',
     'identity_namespace': 'NetEase-campus-75',
     'url_template': 'https://campus.game.163.com/app/detail/index?id={id}&projectId=75'},
    {'name': 'tencent_public', 'company': 'Tencent', 'post_key': 'PostId'},
    {'name': 'netease_public', 'company': 'NetEase', 'post_key': 'id',
     'url_template': 'https://hr.163.com/job-detail.html?id={id}'},
]


def build():
    output = ROOT / 'exports' / 'full_lists'
    output.mkdir(parents=True, exist_ok=True)
    rows, summaries, keys = [], [], set()
    for source in SOURCES:
        directory = ROOT / 'exports' / source['name']
        coverage = json.loads((directory / 'coverage.json').read_text(encoding='utf-8'))
        with (directory / 'jobs.jsonl').open(encoding='utf-8') as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        if len(records) != coverage['record_count']:
            raise ValueError('saved coverage/record count mismatch')
        summary = {key: coverage[key] for key in ('scope', 'list_complete', 'expected_total', 'record_count', 'pages_received', 'jd_complete', 'needs_details_count')}
        summary['source'] = source['name']
        summaries.append(summary)
        for record in records:
            raw, fields = record['raw'], record['fields']
            post_id = str(raw[source['post_key']])
            unique_key = (source.get('identity_namespace', source['company']), post_id)
            repeated = unique_key in keys
            keys.add(unique_key)
            url = record.get('url')
            url_basis = record['url_provenance']
            if not url and source.get('url_template'):
                url = source['url_template'].format(id=quote(post_id, safe=''))
                url_basis = 'constructed_from_public_frontend_route; not individually fetched'
            rows.append({
                'source': source['name'], 'company_label': source['company'],
                'post_id': post_id, 'source_record_id': str(record['id']),
                'title': record['title'], 'project': fields.get('project', ''),
                'recruit_label': fields.get('recruit_label', ''),
                'location': record['location'], 'department': record['dept'],
                'category': record['category'], 'work_type': fields.get('work_type', ''),
                'education': fields.get('education', ''), 'experience': fields.get('experience', ''),
                'url': url, 'url_provenance': url_basis,
                'description': record['description'], 'requirements': record['requirements'],
                'needs_details': record['needs_details'],
                'entry_kind': 'project_aggregate' if post_id.startswith('-') else 'position_record',
                'overlaps_prior_source': repeated,
                'scope': record['scope'], 'raw_ref': record['raw_ref'], 'raw_index': record['raw_index'],
            })
    columns = list(rows[0]) if rows else []
    campus_rows = [row for row in rows if 'campus' in row['source']]
    for name, selected in (('all_lists', rows), ('campus_lists', campus_rows)):
        with (output / f'{name}.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in selected:
                writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                                 for key, value in row.items()})
        with (output / f'{name}.jsonl').open('w', encoding='utf-8') as handle:
            for row in selected:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    result = {
        'sources': summaries, 'source_record_count': len(rows),
        'unique_scoped_post_ids': len(keys), 'campus_source_records': len(campus_rows),
        'cross_source_overlap_records': sum(row['overlaps_prior_source'] for row in rows),
        'project_aggregate_records': sum(row['entry_kind'] == 'project_aggregate' for row in rows),
        'list_complete_within_configured_scopes': all(item['list_complete'] for item in summaries),
        'all_company_channels_complete': False,
        'warnings': ['Only explicitly listed public projects and channels were tested; all-company coverage is not established.',
                     'Tencent campus and public sources overlap; source counts must not be added as unique jobs.',
                     'NetEase IDs are kept in portal/project namespaces; cross-portal equivalence is not assumed.',
                     'Generated detail links are route-based, not individually fetched or verified.',
                     'List completeness does not establish complete job descriptions or application eligibility.'],
    }
    (output / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True))
    return result


if __name__ == '__main__':
    build()
