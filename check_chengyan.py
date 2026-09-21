import sys; sys.path.insert(0, r"c:\DEV\wecom-bot-kiro-cli\workspace\skills")
from sqlalchemy import text
from config.settings import engine
with engine.connect() as c:
    r = c.execute(text("SELECT wecom_userid, wecom_department, wecom_position FROM yami_devops.user_mapping WHERE wecom_userid = 'ChengYan'"))
    for row in r:
        print(row._mapping)
