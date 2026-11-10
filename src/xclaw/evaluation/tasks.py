"""Small synthetic Python tasks, with hidden checks and known-good references.

These are harness regression tasks, not a substitute for real repository benchmarks.
Only seed files and prompts are exposed to the agent; checks and references stay outside work/.
"""

# Embedded fixture programs and assertions intentionally preserve their original text.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from xclaw.evaluation.spec import EvaluationSpec, SuiteSpec


@dataclass
class Task:
    id: str
    category: str
    prompt: str
    seed: dict[str, str]
    checks: list[str]
    reference: dict[str, str]
    history: list[dict[str, str]] = field(default_factory=list)
    acceptance: str | None = None
    allowed_write_paths: list[str] | None = None
    evaluation_requirements: list[str] = field(default_factory=list)
    expected_tool_groups: list[list[str]] = field(default_factory=list)
    evaluation: dict = field(default_factory=dict)
    suite: dict = field(default_factory=dict)
    reference_answer: str | None = None

    @property
    def fingerprint(self):
        # Judge annotations have their own evidence hash, not the agent input baseline hash.
        baseline = asdict(self)
        baseline.pop("evaluation_requirements")
        baseline.pop("expected_tool_groups")
        # Preserve existing experiment fingerprints. Scoring contracts have a separate hash.
        baseline.pop("evaluation")
        baseline.pop("suite")
        if self.reference_answer is None:
            baseline.pop("reference_answer")
        return hashlib.sha256(
            json.dumps(baseline, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()


def _single(name, prompt, seed, checks, reference, category="bug"):
    return Task(name, category, prompt, {"app.py": seed}, checks, {"app.py": reference})


def builtin_tasks() -> list[Task]:
    tasks = [
        _single(
            "clamp",
            "修复 app.clamp，将数字限制在闭区间 [low, high]，low > high 时抛 ValueError。",
            "def clamp(x, low, high):\n    return min(low, max(high, x))\n",
            [
                "assert app.clamp(5, 1, 9) == 5",
                "assert app.clamp(-2, 1, 9) == 1",
                "assert app.clamp(20, 1, 9) == 9",
                "raises(ValueError, app.clamp, 1, 9, 2)",
            ],
            "def clamp(x, low, high):\n    if low > high: raise ValueError()\n    return max(low, min(high, x))\n",
        ),
        _single(
            "mean",
            "修复 app.mean 算术平均值，空列表抛 ValueError。",
            "def mean(values):\n    return sum(values) / (len(values) - 1)\n",
            [
                "assert app.mean([2, 4, 6]) == 4",
                "assert app.mean([7]) == 7",
                "raises(ValueError, app.mean, [])",
            ],
            "def mean(values):\n    if not values: raise ValueError()\n    return sum(values) / len(values)\n",
        ),
        _single(
            "median",
            "修复 app.median：偶数长度取中间两数均值，空输入抛 ValueError，不修改输入。",
            "def median(values):\n    values.sort()\n    return values[len(values)//2]\n",
            [
                "assert app.median([4, 1, 3, 2]) == 2.5",
                "assert app.median([3, 1, 2]) == 2",
                "x=[3,1,2]; app.median(x); assert x == [3,1,2]",
                "raises(ValueError, app.median, [])",
            ],
            "def median(values):\n    if not values: raise ValueError()\n    s=sorted(values); n=len(s)\n    return s[n//2] if n%2 else (s[n//2-1]+s[n//2])/2\n",
        ),
        _single(
            "binary_search",
            "修复 app.binary_search，返回有序列表中目标的索引，未找到返回 -1。",
            "def binary_search(values, target):\n    lo, hi = 0, len(values)-1\n    while lo < hi:\n        mid=(lo+hi)//2\n        if values[mid] == target: return mid\n        if values[mid] < target: lo=mid+1\n        else: hi=mid-1\n    return -1\n",
            [
                "assert app.binary_search([1,3,5],5) == 2",
                "assert app.binary_search([9],9) == 0",
                "assert app.binary_search([],3) == -1",
                "assert app.binary_search([1,4],2) == -1",
            ],
            "def binary_search(values, target):\n    lo,hi=0,len(values)-1\n    while lo<=hi:\n        mid=(lo+hi)//2\n        if values[mid]==target: return mid\n        if values[mid]<target: lo=mid+1\n        else: hi=mid-1\n    return -1\n",
        ),
        _single(
            "palindrome",
            "修复 app.is_palindrome，忽略大小写及非字母数字字符，空字符串为 True。",
            "def is_palindrome(text):\n    return text == text[::-1]\n",
            [
                "assert app.is_palindrome('A man, a plan, a canal: Panama!')",
                "assert not app.is_palindrome('hello')",
                "assert app.is_palindrome('')",
            ],
            "def is_palindrome(text):\n    s=''.join(c for c in text.casefold() if c.isalnum())\n    return s==s[::-1]\n",
        ),
        _single(
            "dedupe",
            "修复 app.dedupe，去重且保持第一次出现的顺序，不改变原列表。",
            "def dedupe(values):\n    return sorted(set(values))\n",
            [
                "assert app.dedupe([3,1,3,2,1]) == [3,1,2]",
                "assert app.dedupe([]) == []",
                "x=[2,1,2]; app.dedupe(x); assert x==[2,1,2]",
            ],
            "def dedupe(values):\n    return list(dict.fromkeys(values))\n",
        ),
        _single(
            "parse_bool",
            "修复 app.parse_bool，忽略大小写及两端空格；true/1/yes 为真，false/0/no 为假，其他字符串抛 ValueError。",
            "def parse_bool(text):\n    return bool(text)\n",
            [
                "assert app.parse_bool(' FALSE ') is False",
                "assert app.parse_bool('Yes') is True",
                "assert app.parse_bool('0') is False",
                "raises(ValueError, app.parse_bool, 'maybe')",
            ],
            "def parse_bool(text):\n    s=text.strip().lower()\n    if s in ('true','1','yes'): return True\n    if s in ('false','0','no'): return False\n    raise ValueError()\n",
        ),
        _single(
            "slugify",
            "修复 app.slugify，小写化，将连续非 ASCII 字母数字替换为一个横杠，去除首尾横杠。",
            "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
            [
                "assert app.slugify(' Hello,   World! ') == 'hello-world'",
                "assert app.slugify('A__B') == 'a-b'",
                "assert app.slugify('!!!') == ''",
            ],
            "import re\ndef slugify(text):\n    return re.sub(r'[^a-z0-9]+','-',text.lower()).strip('-')\n",
        ),
        _single(
            "chunks",
            "修复 app.chunks，按正整数 size 分块，保留最后不足一块的元素；非正整数 size 抛 ValueError。",
            "def chunks(values, size):\n    return [values[i:i+size] for i in range(0,len(values)-size+1,size)]\n",
            [
                "assert app.chunks([1,2,3,4,5],2)==[[1,2],[3,4],[5]]",
                "assert app.chunks([],2)==[]",
                "raises(ValueError, app.chunks, [1], 0)",
                "raises(ValueError, app.chunks, [1], 1.5)",
            ],
            "def chunks(values,size):\n    if type(size) is not int or size<=0: raise ValueError()\n    return [values[i:i+size] for i in range(0,len(values),size)]\n",
        ),
        _single(
            "rotate",
            "修复 app.rotate，将列表向右旋转 k 个位置，支持负数和超过长度的 k，空列表返回空列表，不修改输入。",
            "def rotate(values,k):\n    return values[-k:]+values[:-k]\n",
            [
                "assert app.rotate([1,2,3],4)==[3,1,2]",
                "assert app.rotate([1,2,3],-1)==[2,3,1]",
                "assert app.rotate([],3)==[]",
                "assert app.rotate([1,2],0)==[1,2]",
            ],
            "def rotate(values,k):\n    if not values: return []\n    k%=len(values)\n    return values[-k:]+values[:-k] if k else list(values)\n",
        ),
        _single(
            "factorial",
            "实现 app.factorial(n)，非负整数阶乘；负数、布尔和非整数抛 ValueError。",
            "def factorial(n):\n    raise NotImplementedError\n",
            [
                "assert app.factorial(0)==1",
                "assert app.factorial(6)==720",
                "raises(ValueError, app.factorial, -1)",
                "raises(ValueError, app.factorial, True)",
            ],
            "def factorial(n):\n    if type(n) is not int or n<0: raise ValueError()\n    r=1\n    for i in range(2,n+1): r*=i\n    return r\n",
            "feature",
        ),
        _single(
            "flatten",
            "实现 app.flatten，对嵌套的 list 递归展平，其他元素保持原样。",
            "def flatten(values):\n    raise NotImplementedError\n",
            [
                "assert app.flatten([1,[2,[3]],[],4])==[1,2,3,4]",
                "assert app.flatten(['ab',[None]])==['ab',None]",
                "assert app.flatten([])==[]",
            ],
            "def flatten(values):\n    result=[]\n    for x in values:\n        result.extend(flatten(x) if isinstance(x,list) else [x])\n    return result\n",
            "feature",
        ),
        _single(
            "word_counts",
            "实现 app.word_counts，统计 ASCII 单词，忽略大小写，标点为分隔符。",
            "def word_counts(text):\n    raise NotImplementedError\n",
            [
                "assert app.word_counts('Hello, HELLO world!')=={'hello':2,'world':1}",
                "assert app.word_counts('')=={}",
                "assert app.word_counts('a-b')=={'a':1,'b':1}",
            ],
            "import re\nfrom collections import Counter\ndef word_counts(text):\n    return dict(Counter(re.findall(r'[a-z]+',text.lower())))\n",
            "feature",
        ),
        _single(
            "paginate",
            "实现 app.paginate(values,page,size)，page 从 1 开始，page/size 必须是正整数，否则抛 ValueError。",
            "def paginate(values,page,size):\n    raise NotImplementedError\n",
            [
                "assert app.paginate([1,2,3,4,5],2,2)==[3,4]",
                "assert app.paginate([1],9,2)==[]",
                "raises(ValueError, app.paginate, [1], 0, 2)",
                "raises(ValueError, app.paginate, [1], 1, True)",
            ],
            "def paginate(values,page,size):\n    if type(page) is not int or type(size) is not int or min(page,size)<=0: raise ValueError()\n    return values[(page-1)*size:page*size]\n",
            "feature",
        ),
        _single(
            "duration",
            "实现 app.parse_duration，将 '2h 3m 4s' 等非负整数组合转为秒。允许单位间空格，不允许重复单位或无单位文本；空输入和无效输入抛 ValueError。",
            "def parse_duration(text):\n    raise NotImplementedError\n",
            [
                "assert app.parse_duration('2h 3m 4s')==7384",
                "assert app.parse_duration('0s')==0",
                "raises(ValueError, app.parse_duration, '2m 3m')",
                "raises(ValueError, app.parse_duration, '20')",
            ],
            "import re\ndef parse_duration(text):\n    text=text.strip(); matches=list(re.finditer(r'(\\d+)([hms])',text))\n    if not matches or re.sub(r'\\d+[hms]','',text).strip(): raise ValueError()\n    units=[m[2] for m in matches]\n    if len(set(units))!=len(units): raise ValueError()\n    return sum(int(m[1])*{'h':3600,'m':60,'s':1}[m[2]] for m in matches)\n",
            "feature",
        ),
        _single(
            "merge_config",
            "实现 app.merge_config(base,override)，递归合并 dict，其他值由 override 覆盖，不修改原对象，也不共享嵌套可变对象。",
            "def merge_config(base,override):\n    raise NotImplementedError\n",
            [
                "assert app.merge_config({'a':{'x':1,'y':2}},{'a':{'x':3}})=={'a':{'x':3,'y':2}}",
                "b={'a':[1]}; r=app.merge_config(b,{}); r['a'].append(2); assert b=={'a':[1]}",
                "assert app.merge_config({'a':1},{'a':None})=={'a':None}",
            ],
            "from copy import deepcopy\ndef merge_config(base,override):\n    r=deepcopy(base)\n    for k,v in override.items():\n        r[k]=merge_config(r[k],v) if k in r and isinstance(r[k],dict) and isinstance(v,dict) else deepcopy(v)\n    return r\n",
            "feature",
        ),
    ]
    tasks += [
        Task(
            "invoice",
            "multi_file",
            "修复 pricing.discount 和 app.invoice：discount(price,percent) 返回折后价；invoice(items,discount_percent,tax_percent) 对合计先打折再加税。",
            {
                "pricing.py": "def discount(price,percent):\n    return price*(1+percent/100)\n",
                "app.py": "from pricing import discount\ndef invoice(items,discount_percent,tax_percent):\n    return discount(sum(items),discount_percent)+tax_percent\n",
            },
            [
                "assert pricing.discount(100,20)==80",
                "assert abs(app.invoice([60,40],20,10)-88)<1e-9",
                "assert app.invoice([],0,0)==0",
            ],
            {
                "pricing.py": "def discount(price,percent):\n    return price*(1-percent/100)\n",
                "app.py": "from pricing import discount\ndef invoice(items,discount_percent,tax_percent):\n    return discount(sum(items),discount_percent)*(1+tax_percent/100)\n",
            },
        ),
        Task(
            "currency",
            "multi_file",
            "修复 money.to_cents 和 app.total_cents：用 Decimal 从字符串金额精确换算整数分，拒绝超过两位小数；total_cents 接收字符串列表并返回合计分。",
            {
                "money.py": "def to_cents(text):\n    return int(float(text)*100)\n",
                "app.py": "from money import to_cents\ndef total_cents(values):\n    return sum(float(v) for v in values)\n",
            },
            [
                "assert money.to_cents('0.29')==29",
                "assert app.total_cents(['0.10','0.20'])==30",
                "raises(ValueError,money.to_cents,'1.001')",
            ],
            {
                "money.py": "from decimal import Decimal\ndef to_cents(text):\n    v=Decimal(text)*100\n    if not v.is_finite() or v!=v.to_integral_value(): raise ValueError()\n    return int(v)\n",
                "app.py": "from money import to_cents\ndef total_cents(values):\n    return sum(to_cents(v) for v in values)\n",
            },
        ),
        Task(
            "authentication",
            "multi_file",
            "修复 auth.verify 和 app.login：使用 hmac.compare_digest 比较字符串，不允许空的 stored；login 返回严格布尔，未知用户为 False。",
            {
                "auth.py": "def verify(candidate,stored):\n    return candidate in stored\n",
                "app.py": "from auth import verify\ndef login(users,name,password):\n    return users.get(name)\n",
            },
            [
                "assert auth.verify('secret','secret') is True",
                "assert auth.verify('sec','secret') is False",
                "assert auth.verify('','') is False",
                "assert app.login({'u':'pw'},'u','pw') is True",
                "assert app.login({'u':'pw'},'x','pw') is False",
                "assert 'compare_digest' in inspect.getsource(auth.verify)",
            ],
            {
                "auth.py": "import hmac\ndef verify(candidate,stored):\n    return bool(stored) and hmac.compare_digest(candidate.encode(),stored.encode())\n",
                "app.py": "from auth import verify\ndef login(users,name,password):\n    return bool(name in users and verify(password,users[name]))\n",
            },
        ),
        Task(
            "geometry",
            "multi_file",
            "修复 geometry.rectangle_area、rectangle_perimeter 以及 app.describe；矩形面积 w*h、周长 2*(w+h)，负边长抛 ValueError，describe 返回 area/perimeter 字典。",
            {
                "geometry.py": "def rectangle_area(w,h):\n    return w+h\ndef rectangle_perimeter(w,h):\n    return w*h\n",
                "app.py": "from geometry import rectangle_area,rectangle_perimeter\ndef describe(w,h):\n    return {'area':rectangle_perimeter(w,h),'perimeter':rectangle_area(w,h)}\n",
            },
            [
                "assert geometry.rectangle_area(3,4)==12",
                "assert geometry.rectangle_perimeter(3,4)==14",
                "assert app.describe(2,5)=={'area':10,'perimeter':14}",
                "raises(ValueError,geometry.rectangle_area,-1,2)",
                "raises(ValueError,geometry.rectangle_perimeter,2,-1)",
            ],
            {
                "geometry.py": "def rectangle_area(w,h):\n    if min(w,h)<0: raise ValueError()\n    return w*h\ndef rectangle_perimeter(w,h):\n    if min(w,h)<0: raise ValueError()\n    return 2*(w+h)\n",
                "app.py": "from geometry import rectangle_area,rectangle_perimeter\ndef describe(w,h):\n    return {'area':rectangle_area(w,h),'perimeter':rectangle_perimeter(w,h)}\n",
            },
        ),
    ]
    contexts = [
        (
            "context_decimal",
            "金额必须使用 Decimal，禁止 round/float，输入输出都是字符串，结果保留两位小数。",
            "实现 app.add_money(a,b)，满足我们之前约定的金额计算规则。",
            [
                "assert app.add_money('0.10','0.20')=='0.30'",
                "assert app.add_money('123456789012345.67','0.01')=='123456789012345.68'",
            ],
            "from decimal import Decimal\ndef add_money(a,b):\n    return format(Decimal(a)+Decimal(b),'.2f')\n",
        ),
        (
            "context_case",
            "用户 ID 区分大小写，lookup 必须精确匹配，未找到返回 None，不允许修改输入字典。",
            "实现 app.lookup(users,user_id)，按之前约定处理用户 ID。",
            [
                "assert app.lookup({'Alice':1,'alice':2},'Alice')==1",
                "assert app.lookup({'Alice':1},'ALICE') is None",
            ],
            "def lookup(users,user_id):\n    return users.get(user_id)\n",
        ),
        (
            "context_validation",
            "年龄必须为 0 到 150 的整数；布尔、字符串、负数、超过上限都抛 ValueError。",
            "实现 app.validate_age(age)，按照之前的校验规则，合法时返回原值。",
            [
                "assert app.validate_age(150)==150",
                "raises(ValueError,app.validate_age,True)",
                "raises(ValueError,app.validate_age,'18')",
                "raises(ValueError,app.validate_age,151)",
            ],
            "def validate_age(age):\n    if type(age) is not int or not 0<=age<=150: raise ValueError()\n    return age\n",
        ),
        (
            "context_order",
            "排序按 score 降序，相同分数保持输入顺序，不修改原列表；不要用名字作次级排序。",
            "实现 app.rank(records)，遵守之前约定的排序规则。",
            [
                "x=[{'name':'z','score':2},{'name':'a','score':2},{'name':'b','score':3}]; r=app.rank(x); assert [v['name'] for v in r]==['b','z','a']; assert x[0]['name']=='z'"
            ],
            "def rank(records):\n    return sorted(records,key=lambda r:r['score'],reverse=True)\n",
        ),
    ]
    for name, constraint, prompt, checks, reference in contexts:
        task = _single(name, prompt, "# 实现最终用户请求要求的函数\n", checks, reference, "context")
        task.history = [
            {"role": "user", "content": constraint},
            {"role": "assistant", "content": "已记录，后续实现会遵守。"},
        ]
        # Repeated neutral discussion intentionally puts the constraint outside recent history.
        for index in range(20):
            task.history.extend(
                [
                    {
                        "role": "user",
                        "content": f"第 {index} 次无关讨论。"
                        + "讨论目录命名和开发安排，这些材料没有新增函数需求，也没有改变已约定的规则。"
                        * 24,
                    },
                    {"role": "assistant", "content": "这部分仅为讨论记录，不更改先前约束。" * 8},
                ]
            )
        tasks.append(task)
    for task in tasks:
        task.evaluation_requirements = (
            [task.history[0]["content"], task.prompt] if task.history else [task.prompt]
        )
        task.expected_tool_groups = [
            ["read_file", "search_in_files", "list_directory", "bash"],
            ["edit_file", "write_file", "bash"],
        ]
    return tasks


def validation_script(task: Task) -> str:
    if task.acceptance is not None:
        return task.acceptance
    modules = sorted(name[:-3] for name in task.seed if name.endswith(".py"))
    return (
        "import sys, inspect, importlib, unittest\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "def raises(error, fn, *args):\n"
        "    try: fn(*args)\n"
        "    except error: return\n"
        "    raise AssertionError('expected '+error.__name__)\n"
        + "\n".join(f"{name}=importlib.import_module({name!r})" for name in modules)
        + "\n"
        + f"checks={task.checks!r}\n"
        "suite=unittest.TestSuite()\n"
        "for index, check in enumerate(checks):\n"
        "    suite.addTest(unittest.FunctionTestCase(lambda code=check: exec(code, globals()), description=str(index)))\n"
        "result=unittest.TextTestRunner(verbosity=2).run(suite)\n"
        "if result.wasSuccessful(): print('XCLAW_VALIDATION_OK:'+str(result.testsRun))\n"
        "sys.exit(0 if result.wasSuccessful() else 1)\n"
    )


def load_tasks(path: str | Path) -> list[Task]:
    """Load user-owned UTF-8 repository fixtures and validators from an explicit suite.

    Validators run in a fresh container with work/ read-only. To pass they must exit 0,
    print XCLAW_VALIDATION_OK:1, and use sys.argv[1] as the workspace path.
    """
    from xclaw.core.workspace import _files

    manifest = Path(path).resolve()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or "tasks" not in data
        or set(data) - {"tasks", "suite"}
        or not isinstance(data["tasks"], list)
    ):
        raise ValueError("suite 必须为包含 tasks 列表的 JSON 对象")
    suite = SuiteSpec.model_validate(data.get("suite", {})).model_dump()
    tasks = []
    seen = set()
    required = {"id", "category", "prompt", "fixture", "validator", "allowed_write_paths"}
    for entry in data["tasks"]:
        if (
            not isinstance(entry, dict)
            or not required <= entry.keys()
            or entry.keys()
            - required
            - {"history", "evaluation_requirements", "expected_tool_groups", "evaluation"}
        ):
            raise ValueError("suite task 字段缺失或包含未知字段")
        if (
            not isinstance(entry["id"], str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", entry["id"])
            or entry["id"] in seen
        ):
            raise ValueError("任务 ID 非法或重复")
        seen.add(entry["id"])
        fixture = (manifest.parent / entry["fixture"]).resolve(strict=True)
        if not fixture.is_dir():
            raise ValueError("fixture 必须为目录")
        paths = _files(fixture)
        if len(paths) > 10000 or sum(p.stat().st_size for p in paths.values()) > 100_000_000:
            raise ValueError("fixture 超过 10000 文件或 100MB 上限")
        files = {name: file.read_text(encoding="utf-8") for name, file in paths.items()}
        validator = (manifest.parent / entry["validator"]).resolve(strict=True)
        if validator.is_relative_to(fixture):
            raise ValueError("独立验收脚本不能位于 Agent 可见的 fixture 中")
        acceptance = validator.read_text(encoding="utf-8")
        scopes = entry["allowed_write_paths"]
        if (
            not isinstance(scopes, list)
            or not scopes
            or not all(isinstance(s, str) for s in scopes)
        ):
            raise ValueError("allowed_write_paths 必须是非空字符串列表")
        tasks.append(
            Task(
                entry["id"],
                entry["category"],
                entry["prompt"],
                files,
                ["external_validator"],
                {},
                entry.get("history", []),
                acceptance,
                scopes,
            )
        )
        annotations = entry.get("evaluation_requirements", [])
        groups = entry.get("expected_tool_groups", [])
        if not isinstance(annotations, list) or not all(
            isinstance(s, str) and s for s in annotations
        ):
            raise ValueError("evaluation_requirements 必须是非空文本的列表")
        if not isinstance(groups, list) or not all(
            isinstance(g, list) and g and all(isinstance(s, str) and s for s in g) for g in groups
        ):
            raise ValueError("expected_tool_groups 必须是工具名分组列表")
        tasks[-1].evaluation_requirements = annotations
        tasks[-1].expected_tool_groups = groups
        tasks[-1].evaluation = EvaluationSpec.model_validate(entry.get("evaluation", {})).model_dump()
        tasks[-1].suite = suite
    if not tasks:
        raise ValueError("suite 不能为空")
    return tasks
