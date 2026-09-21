import base64
data = open('/tmp/push_b64.txt').read().strip()
content = base64.b64decode(data)
open('/opt/andon-reminder/andon_icqa_push.py', 'wb').write(content)
print('deployed', len(content), 'bytes')
# verify
text = open('/opt/andon-reminder/andon_icqa_push.py').read()
print('SKILLS_DIR line:', [l for l in text.splitlines() if 'SKILLS_DIR' in l])
