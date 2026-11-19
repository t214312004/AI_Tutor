"""Original starter examples; require local educator review, never claim textbook alignment."""

CARDS = (
    {'id':'carry-add','grades':[1,2,3,4],'subject':'數學','keywords':['加法','進位','+','＋'],
     'goal':'理解十個一可以換成一個十，完成進位加法',
     'prerequisite':'能分辨十位、個位並把數拆開',
     'misconception':'個位超過九時漏進位，或把進位加錯位',
     'representation':'十格、位值表或十根一捆的小棒',
     'example':'27＋15：7＋5是12，換成1個十和2個一，再合併十位，得到42。',
     'check':{'question':'換一題：36＋18，請自己算並說明進位放在哪裡。','answer':'54；個位14換成1個十和4個一。'}},
    {'id':'borrow-subtract','grades':[2,3,4],'subject':'數學','keywords':['減法','退位','借位','-','－'],
     'goal':'理解退一個十換成十個一', 'prerequisite':'十位與個位的位值',
     'misconception':'只把個位加十，忘了十位減一', 'representation':'位值表與交換小棒',
     'example':'52－18：5個十2個一換成4個十12個一，12－8＝4、4－1＝3，得到34。',
     'check':{'question':'換一題：61－27，請自己算並說明交換。','answer':'34'}},
    {'id':'same-denominator','grades':[3,4,5],'subject':'數學','keywords':['分數','分母','等分'],
     'goal':'比較同一整體、同分母的分數', 'prerequisite':'分母表示等分數，分子表示取幾份',
     'misconception':'比較不同大小的整體，或以為分子越大每份也越大', 'representation':'兩條一樣長的等分紙帶',
     'example':'同樣大小的餅都分成8等份，5/8比3/8多2份，所以5/8較大。',
     'check':{'question':'同一整體下，4/7和6/7哪個大？請說理由。','answer':'6/7；每份同樣大，取的份數較多。'}},
    {'id':'rectangle-area','grades':[3,4,5],'subject':'數學','keywords':['面積','平方','長方形'],
     'goal':'用單位方格解釋長方形面積', 'prerequisite':'會數每排格數與排數',
     'misconception':'把周長當面積，或漏寫平方單位', 'representation':'每格1平方公分的方格圖',
     'example':'每排4格、共3排，面積4×3＝12平方公分；不是把四邊長相加。',
     'check':{'question':'每排5格、共4排，每格1平方公分，面積多少？','answer':'20平方公分'}},
    {'id':'reading-evidence','grades':[1,2,3,4,5,6],'subject':'國語文','keywords':['閱讀','文章','句子'],
     'goal':'根據文中線索回答並指出依據', 'prerequisite':'讀懂關鍵句，分辨文中資訊與推測',
     'misconception':'只憑生活經驗猜測，不回到文章', 'representation':'標記人物、動作及原因的短句',
     'example':'「小安帶著雨傘出門，因為外面正在下雨。」問帶傘原因，依據是「外面正在下雨」。',
     'check':{'question':'「小美把窗戶關上，因為風太大。」她為什麼關窗？指出依據。','answer':'風太大；依據是「因為風太大」。'}},
    {'id':'english-recall','grades':[1,2,3,4,5,6],'subject':'英語文','keywords':['單字','拼字','apple'],
     'goal':'先理解單字，再遮住示範回想', 'prerequisite':'知道單字意思與字母名稱',
     'misconception':'看著範本抄對就當成能獨立拼寫', 'representation':'圖像、口述與可遮蔽的字母',
     'example':'用apple示範「蘋果」的意思與a-p-p-l-e；遮住後請孩子自行回想，再核對。',
     'check':{'question':'另一次短檢查先遮住範本，再拼出「蘋果」的英文。','answer':'apple'}},
)


def teaching_cards(grade, text, allowed_ids=None):
    from .curriculum_map import detect_subject
    subject=detect_subject(text)
    lowered = text.lower()
    allowed = set(allowed_ids) if allowed_ids is not None else None
    matches = [card for card in CARDS if grade in card['grades']
               and (allowed is None or card['id'] in allowed)
               and (not subject or subject==card['subject'])
               and any(word.lower() in lowered for word in card['keywords'])]
    return [{**card, 'review':'draft_requires_parent_or_teacher_review',
             'basis':'本程式原創教學示例，非出版社教材或經人工審定課綱；先核對適用程度。'} for card in matches[:2]]
