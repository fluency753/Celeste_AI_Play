## 辅助程序

{program_lead}{cpu_rule}{parallel_rule}
{explore_program_rule}用 /workspace/game/show.py 的 status() 报告真实进度。
/workspace 是较慢的主机挂载目录：批量计算前将数据读入内存或拷到 /tmp，循环中不反复读写工作区，模拟器对象尽量只构造一次再复用。
互不依赖的小检查可以合并或并行，依赖结果的步骤分开。{save_context_print}
