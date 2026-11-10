"""
记忆系统的测试
"""

from xclaw.core.memory import ConversationMemory, WorkingMemory


class TestConversationMemory:
    """对话记忆的测试"""

    def test_remember_and_recall(self):
        """测试记忆的存和取"""
        memory = ConversationMemory()
        memory.remember("user_name", "张三")
        assert memory.recall("user_name") == "张三"

    def test_recall_nonexistent(self):
        """测试检索不存在的记忆"""
        memory = ConversationMemory()
        assert memory.recall("nonexistent") is None

    def test_recall_latest(self):
        """测试获取最新的值"""
        memory = ConversationMemory()
        memory.remember("key", "value1")
        memory.remember("key", "value2")
        assert memory.recall("key") == "value2"

    def test_remember_event(self):
        """测试事件记录"""
        memory = ConversationMemory()
        memory.remember_event("文件已读取")
        memory.remember_event("代码已生成")

        events = memory.get_recent_events(10)
        assert "文件已读取" in events
        assert "代码已生成" in events

    def test_format_for_prompt(self):
        """测试格式化为 Prompt"""
        memory = ConversationMemory()
        memory.remember("user_name", "张三")
        memory.remember_event("读取了 main.py")

        formatted = memory.format_for_prompt()
        assert "user_name" in formatted
        assert "张三" in formatted
        assert "main.py" in formatted

    def test_empty_format(self):
        """测试空记忆的格式化"""
        memory = ConversationMemory()
        assert memory.format_for_prompt() == ""

    def test_max_size(self):
        """测试最大容量"""
        memory = ConversationMemory(max_size=3)
        memory.remember("k1", "v1")
        memory.remember("k2", "v2")
        memory.remember("k3", "v3")
        memory.remember("k4", "v4")

        assert len(memory) == 3
        assert memory.recall("k1") is None  # 最旧的被移除了

    def test_clear(self):
        """测试清空记忆"""
        memory = ConversationMemory()
        memory.remember("k1", "v1")
        memory.clear()
        assert len(memory) == 0
        assert memory.format_for_prompt() == ""

    def test_save_and_load(self, tmp_path):
        """测试 KV 长期记忆跨进程文件恢复。"""
        path = tmp_path / "session.memory.json"
        memory = ConversationMemory()
        memory.remember("language", "Python")
        memory.remember_event("选择 asyncio")
        memory.save(str(path))

        restored = ConversationMemory()
        restored.load(str(path))

        assert restored.recall("language") == "Python"
        assert restored.get_recent_events() == ["选择 asyncio"]
        assert not path.with_suffix(".json.tmp").exists()


class TestWorkingMemory:
    """工作记忆的测试"""

    def test_set_and_get(self):
        """测试设置和获取状态"""
        wm = WorkingMemory()
        wm.set("current_step", "step_1")
        assert wm.get("current_step") == "step_1"

    def test_get_default(self):
        """测试默认值"""
        wm = WorkingMemory()
        assert wm.get("unknown", "default") == "default"

    def test_save_result(self):
        """测试保存步骤结果"""
        wm = WorkingMemory()
        wm.save_result("step_1", "完成")
        assert wm.get_result("step_1") == "完成"

    def test_errors(self):
        """测试错误记录"""
        wm = WorkingMemory()
        assert not wm.has_errors()

        wm.add_error("连接超时")
        assert wm.has_errors()
        assert len(wm._errors) == 1

    def test_clear(self):
        """测试清空工作记忆"""
        wm = WorkingMemory()
        wm.set("key", "value")
        wm.save_result("s1", "r1")
        wm.add_error("err1")
        wm.clear()

        assert wm.get("key") is None
        assert wm.get_result("s1") is None
        assert not wm.has_errors()

    def test_summary(self):
        """测试生成摘要"""
        wm = WorkingMemory()
        wm.set("task", "分析代码")
        wm.save_result("s1", "完成")

        summary = wm.summary()
        assert "分析代码" in summary
