import asyncio
import time
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest

@register("astrbot_plugin_auto_status", "ZZTale", "QQ状态自动设置与更改", "1.0.0")
class AutoStatusPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)

        self.config = config or {}
        self.current_status = {}  
        self.last_change_time = {}  
        self.retry_tracker = {}  
        self._processed_platforms = set()
        
        # 1. 读取全局权限白名单
        self.allowed_qq = [str(qq) for qq in self.config.get("allowed_qq", [])]
        
        # 2. 读取开机自启配置块
        startup_cfg = self.config.get("startup_config", {})
        self.enable_startup_status = startup_cfg.get("enable_startup_status", True)
        self.status_change_whitelist = startup_cfg.get("status_change_whitelist", False)
        self.whitelist = startup_cfg.get("whitelist", [])
        self.blacklist = startup_cfg.get("blacklist", [])

        # 3. 读取聊天状态修改配置块
        chat_cfg = self.config.get("chat_config", {})
        self.max_retry = max(0, int(chat_cfg.get("max_retry", 1)))
        self.cooldown_seconds = int(chat_cfg.get("cooldown_seconds", 30))

        # 4. 解析状态映射表
        self.status_map = {}
        raw_mapping = chat_cfg.get("status_mapping", [])
        for item in raw_mapping:
            try:
                # 按逗号分割，比如 "摸鱼中,10,1300" -> ["摸鱼中", "10", "1300"]
                parts = [p.strip() for p in item.split(",")]
                if len(parts) >= 3:
                    name = parts[0]
                    status_code = int(parts[1])
                    third_param = parts[2]
                    
                    # 判断：如果第3个参数是纯数字，说明是标准扩展码
                    if third_param.isdigit():
                        ext_code = int(third_param)
                        self.status_map[name] = {
                            "type": "standard", 
                            "status": status_code, 
                            "ext": ext_code
                        }
                    else:
                        # 如果不是纯数字，说明是自定义文字（进入DIY模式）
                        wording = third_param
                        # 第4个参数是图标ID，如果没填，默认给个 14（可爱）
                        face_id = int(parts[3]) if len(parts) >= 4 else 14
                        self.status_map[name] = {
                            "type": "diy", 
                            "status": status_code, 
                            "wording": wording, 
                            "face_id": face_id
                        }
            except Exception as e:
                logger.error(f"【自动状态插件】解析状态映射失败 '{item}': {e}")

        logger.info(f"【自动状态插件】已加载 {len(self.status_map)} 个可用状态: {list(self.status_map.keys())}")
    
    async def initialize(self):
        """生命周期方法：AstrBot 启动时自动执行"""
        logger.info("【自动状态插件】启动中，等待系统稳定...")
    
    async def _try_set_status(self, bot_id: str):
        """为单个机器人独立重试设置状态"""
        self._processed_platforms.add(bot_id)
        for attempt in range(5): 
            success = await self._set_status_by_platform(bot_id, "在线")
            if success:
                logger.info(f"【自动状态插件】机器人 {bot_id} 已成功设为在线")
                return
            await asyncio.sleep(5)  
        logger.warning(f"【自动状态插件】机器人 {bot_id} 重试 5 次仍未成功，暂时放弃（等待重连触发）")
        self._processed_platforms.discard(bot_id)
    
    @filter.on_platform_loaded()
    async def on_platform_loaded(self):
        """每启动一个平台触发一次，为尚未处理的平台启动独立重试任务"""
        if not self.enable_startup_status:
            return

        for platform in self.context.platform_manager.platform_insts:
            if platform.meta().name == "aiocqhttp":
                bot_id = platform.meta().id
                if bot_id in self._processed_platforms:
                    continue
                # 黑白名单判断
                if self.status_change_whitelist:
                    if bot_id not in self.whitelist:
                        continue
                else:
                    if bot_id in self.blacklist:
                        continue
                # 为这个机器人启动独立的重试任务
                asyncio.create_task(self._try_set_status(bot_id))

    async def terminate(self):
        """生命周期方法：AstrBot 关闭时自动执行"""
        logger.info("【自动状态插件】正在退出...")

    # ==================== 动态注入状态列表 ====================
    
    @filter.on_llm_request()
    async def inject_status_list(self, event: AstrMessageEvent, req: ProviderRequest):
        """在每次 AI 思考前，偷偷把当前可用的状态列表塞进系统提示词里"""
        if not self.status_map:
            return
            
        available = ", ".join(self.status_map.keys())
        # 将提示追加到系统提示词的最后，告诉 AI 它的可选范围
        prompt = f"""
[系统提示：你当前可以通过工具切换的QQ在线状态有：[{available}]。
【匹配规则】：
1. 切换状态时，请严格从上述列表中提取原词。
2. 如果用户说的词（如“困了”、“摸鱼”）不在列表中，请务必在列表中寻找**意思最相近**的词（如“睡眠”、“摸鱼中”），并填入那个相近的词。
3. 如果用户说的词在列表中有**多个**意思相近的选项（如同时有“摸鱼前”和“摸鱼中”），请放弃调用工具，直接用文字反问用户到底选哪一个。
]
"""
        req.system_prompt += prompt

    # ==================== 底层执行模块 ====================

    async def _set_status_by_platform(self, platform_id: str, status_name: str) -> bool:
        """底层执行修改状态的通用方法：精确匹配 platform_id"""
        for platform in self.context.platform_manager.platform_insts:
            # 精确匹配：只有当前请求的机器人 ID 和遍历到的机器人 ID 一致时才执行
            if platform.meta().id == platform_id:
                try:
                    if status_name not in self.status_map:
                        return False

                    info = self.status_map[status_name]

                    # 根据解析的 type 决定调哪个接口
                    if info["type"] == "standard":
                        # 核心 API 调用：同时传入 status 和 ext_status
                        await platform.bot.call_action(
                            "set_online_status", 
                            status=info["status"],
                            ext_status=info["ext"],
                            battery_status=100
                        )
                        log_msg = f"状态={info['status']}, ext={info['ext']}"
                    else:
                        # 自定义状态
                        await platform.bot.call_action(
                            "set_diy_online_status", 
                            status=info["status"],
                            wording=info["wording"],
                            face_id=info["face_id"]
                        )
                        log_msg = f"文字='{info['wording']}', face_id={info['face_id']}"

                    # 更新本地缓存和冷却时间
                    self.current_status[platform_id] = status_name
                    self.last_change_time[platform_id] = time.time()
                    logger.info(f"【自动状态插件】✅ 机器人 [{platform_id}] 状态已设置为：{status_name} ({log_msg})")
                    return True
                except Exception as e:
                    error_msg = str(e)
                    if "not connected" in error_msg.lower() or "connection" in error_msg.lower() or error_msg == "":
                        logger.debug(f"【自动状态插件】等待机器人 [{platform_id}] 连接中...")
                    else:
                        logger.error(f"【自动状态插件】❌ 机器人 [{platform_id}] 设置状态失败: {e}")
                    return False
        return False

    # ==================== 供 AI 调用的功能模块 ====================

    @filter.llm_tool(name="change_qq_status")
    async def change_qq_status(self, event: AstrMessageEvent, status_name: str):
        '''
        修改【你自己】（QQ机器人）的在线状态。
        
        ⚠️【触发条件】：
        只有当用户明确要求【你】去睡觉、休息，或改变其他状态（例如：“你去睡觉吧”、“晚安，你休息吧”、“把状态设置为在线”），或者你们互道晚安，对话结束时触发。
        
        🚫【严禁触发的情况】：
        如果用户只是表达【自己】累了、困了（例如：“我好困”、“我去睡了”），这属于用户的状态，【绝对不要】调用此工具修改你自己的状态。
        
        Args:
            status_name(string): 目标状态名称。必须从系统提示提供的列表中选择。绝对禁止生造词汇。
        '''
        # 1. 权限拦截：只有主人的 QQ 号才能改状态
        sender_id = str(event.get_sender_id())
        if sender_id not in self.allowed_qq:
            return "系统提示：你无权修改我的状态。"

        # 2. 精准路由：获取当前正在跟用户对话的这个机器人的平台 ID
        current_platform_id = event.get_platform_id()
        # 🚨 获取当前会话的唯一标识（比如某个群的群号），用于记录是谁在犯错
        session_id = event.unified_msg_origin

        # 3. 🛡️ 同义词兜底映射：防止大模型乱造词
        synonym_map = {
            "睡觉": "睡眠", "困了": "睡眠", "休息": "睡眠",
            "忙": "忙碌", "勿扰": "请勿打扰", "工作中": "忙碌",
            "回来": "在线", "上线": "在线"
        }

        # 如果 AI 传的词不在标准表里，去同义词表里找找
        if status_name not in self.status_map:
            if status_name in synonym_map and synonym_map[status_name] in self.status_map:
                status_name = synonym_map[status_name]
            else:
                current_retry = self.retry_tracker.get(session_id, 0)
                # 🚨 硬件级拦截：如果错误次数超过限定次数，直接死锁，不再给 AI 机会
                if current_retry >= self.max_retry:
                    logger.warning(f"【自动状态插件】LLM 在会话 {session_id} 中达到最大试错次数 ({self.max_retry})，强制终止。")
                    # 清空记仇本，防止影响下一次正常对话
                    self.retry_tracker[session_id] = 0 
                    return "系统提示：执行失败！尝试次数到达限制，本次修改已取消。请停止调用工具，直接用文字向用户解释。"
                
                else:
                    # 还没到上限，记仇本 +1，允许重试
                    self.retry_tracker[session_id] = current_retry + 1
                    remain = self.max_retry - current_retry
                    logger.warning(f"【自动状态插件】LLM 传入了无法识别的状态 '{status_name}'，触发重试 (剩余 {remain} 次)。")
                    return (
                        f"系统提示：无法识别状态 '{status_name}'。\n"
                        f"你还有 {remain} 次重试机会。请检查当前状态词是否属于现有的状态表。如果有意思相近的词，请直接替换后【再次调用工具】。\n"
                        f"如果没有匹配或存在歧义，请【立刻停止调用工具】，直接用文字回复用户并说明原因。"
                    )
        
        self.retry_tracker[session_id] = 0 

        # 4. 状态去重：如果当前已经是这个状态，就不重复请求 API
        if self.current_status.get(current_platform_id) == status_name:
            return f"系统提示：当前已经是【{status_name}】状态，无需重复设置。"

        # 5. 冷却限制：防止 AI 在 30 秒内疯狂触发 API
        last_time = self.last_change_time.get(current_platform_id, 0)
        if time.time() - last_time < self.cooldown_seconds:
            return "系统提示：状态刚刚才修改过，请勿频繁更改（冷却中）。"

        # 6. 执行修改
        success = await self._set_status_by_platform(current_platform_id, status_name)
        
        if success:
            return f"系统提示：已成功将你的QQ状态修改为【{status_name}】。请用自然的语气回复用户。"
        else:
            return "系统提示：状态修改失败，可能因为底层连接断开或找不到对应的机器人。"

    # ==================== 手动兜底指令 ====================

    @filter.command("force")
    async def force_status(self, event: AstrMessageEvent, status_name: str = ""):
        """手动触发一键强制状态（管理员指令：/force 状态名）"""
        # 1. 权限拦截：使用我们自己的 allowed_qq 白名单
        sender_id = str(event.get_sender_id())
        if sender_id not in self.allowed_qq:
            yield event.plain_result("系统提示：你无权使用一键修改指令。")
            return

        # 2. 参数校验
        status_name = status_name.strip()
        if not status_name:
            yield event.plain_result("系统提示：请指定要切换的状态。例如：/force 在线")
            return

        # 3. 🚨 严格校验：只认配置里的原词，绝不猜同义词！
        if status_name not in self.status_map:
            available = "、".join(self.status_map.keys())
            yield event.plain_result(f"❌ 错误：无法识别状态 '{status_name}'。\n当前可用状态只有：[{available}]")
            return

        # 4. 开始遍历并执行
        success_count = 0
        total_connected = 0

        for platform in self.context.platform_manager.platform_insts:
            if platform.meta().name == "aiocqhttp":
                total_connected += 1
                bot_id = platform.meta().id
                
                # 复用黑白名单判断逻辑
                if self.status_change_whitelist:
                    if bot_id not in self.whitelist:
                        continue
                else:
                    if bot_id in self.blacklist:
                        continue

                success = await self._set_status_by_platform(bot_id, status_name)
                if success:
                    success_count += 1

        # 5. 反馈结果
        if success_count > 0:
            yield event.plain_result(f"✅ 强制干预成功：已强制将 {success_count} 个机器人设为【{status_name}】。")
        else:
            yield event.plain_result(f"❌ 强制干预失败：当前连上了 {total_connected} 个机器人，但没有符合黑白名单规则的机器人。")