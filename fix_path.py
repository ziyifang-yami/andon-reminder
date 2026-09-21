path = '/opt/andon-reminder/andon_icqa_push.py'
with open(path) as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'SKILLS_DIR' in line and 'DEV' in line:
        print(f"Line {i+1}: {repr(line)}")
        lines[i] = "SKILLS_DIR = Path('/opt/wecom-bot-kiro-cli/workspace/skills')\n"
        print(f"Replaced:  {repr(lines[i])}")

with open(path, 'w') as f:
    f.writelines(lines)
print("done")
