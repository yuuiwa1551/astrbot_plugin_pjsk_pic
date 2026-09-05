"""Run inside the sidecar; only print counts, never response bodies or tokens."""
import json
import os
import urllib.request

base = 'http://127.0.0.1:18060'
headers = {'Authorization': 'Bearer ' + os.environ['AUTH_TOKEN'], 'Content-Type': 'application/json'}
def request(path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)['data']

pages = []
for page in (1, 2):
    result = request('/api/v1/feeds/search', {
        'keyword': '初音未来', 'page': page, 'page_size': 20,
        'filters': {'sort_by': '最新', 'note_type': '图文', 'publish_time': '不限'},
    })
    pages.append(result)
ids = [{x['id'] for x in page['feeds']} for page in pages]
print(json.dumps({'page_counts': [len(x) for x in ids], 'overlap': len(ids[0] & ids[1]),
                  'has_more': [x['hasMore'] for x in pages]}), flush=True)
hit = pages[1]['feeds'][0]
note = request('/api/v1/feeds/detail', {'feed_id': hit['id'], 'xsec_token': hit['xsecToken']})['data']['note']
print(json.dumps({'detail_images': len(note['imageList']), 'topics': len(note['tagList'])}), flush=True)
recent = request('/api/v1/feeds/search', {
    'keyword': '初音未来', 'page': 1, 'page_size': 20,
    'filters': {'sort_by': '最新', 'note_type': '图文', 'publish_time': '一周内'},
})
print(json.dumps({'recent_count': len(recent['feeds'])}), flush=True)
