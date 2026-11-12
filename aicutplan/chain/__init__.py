# [L1 cut-first 主链] 「画面先定」流水线的全部阶段脚本
# 顺序：subs_extract/asr_local(台词源) → causal_graph(因果) → cutfirst_plan(选窗)
#       → narrate_cut(旁白后写) → elastic_fit(弹性对齐) → qc_visual/qc_loop(质检闭环)
#       由 cf_pipeline 编排；工程本地的 cut/r5/r6/r7 脚本在工程目录，不在本包。
