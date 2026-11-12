# [L1 共享库层] 新旧两条链路共同依赖的数据层/领域库
# 模块：narration（旁白数据层/LLM客户端）story_parse（剧情解析）
#       smart_align（句级事件对齐）asr_pipeline（译中缓存）evidence_feed（证据回喂）
# 约束：本层只依赖 L0（aicutplan 包根的 paths/io/media/cache/contracts），
#       不得反向依赖 chain/webui —— 否则会形成导入环。
