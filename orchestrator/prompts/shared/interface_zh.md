# 工具与相关文件

以下只列本次可用入口；参数和当前限制以实际工具声明及最新反馈为准。详细格式在需要时查阅各工具下面的参考资料。

<!-- tool:observe -->
## {tool:observe}：当前观测

获取状态与画面。默认观测可对已核实的过渡和演出有界等待，缓存详情查询不推进游戏。世界坐标以像素计，x 向右、y 向下；速度为像素/秒，60 帧/秒。矩形坐标是左上角，玩家位置是碰撞箱底边中点；站立碰撞箱宽 8 高 11，下蹲高 6。

碰撞图：{collision_image_when}同帧分色图；核对帧号与数据一致，图像不预测动态障碍。

{observe_files_rule}
{observe_reference_rule}
<!-- /tool:observe -->

<!-- tool:act -->
## {tool:act}：执行动作

执行显式输入并返回新观测；提前停下后，剩余输入不会继续执行。默认输入 C 确认，J 跳、X 冲刺、G 抓墙；再次按下须先松键。参数、输入格式和帧数范围见工具声明。

{auto_handling}

{act_files_rule}
{act_reference_rule}
<!-- /tool:act -->

<!-- tool:narrate -->
## {tool:narrate}：公开反馈

不推进游戏。用简短中文{narrate_target}当前判断和下一步；正式／临时思考的显示位置遵循本次反馈约定。
<!-- /tool:narrate -->

<!-- tool:set_effort -->
## {tool:set_effort}：推理强度

当前模型及可选档位见观测头部；调整后直接结束本轮，等待主控以新强度继续。
<!-- /tool:set_effort -->

<!-- tool:stop_run -->
## {tool:stop_run}：报告阻塞

仅在任务所必需的步骤仍无法执行、适用的获准恢复方式已尝试或确知不可用、且需要用户介入时报告。说明当前状态、尝试和结果，以及需要用户做什么。若主控返回恢复步骤，按反馈继续；报告被接受并暂停时结束本轮等用户处理。普通死亡、尚可尝试的路线、单次工具拒绝或自动等待交回控制，不足以判定阻塞。

恢复试玩后按最新用户说明和观测重新判断。反复 observe 看到同一冻结帧不算恢复试验；有可执行的剧情输入节点或获准的过渡处理时，继续该步骤。
watch 不是通用等待；“没有待处理剧情”不代表死亡或重试后的恢复路径不可用。已知刚死亡或重试、角色暂缺时，核对画面后选择确认或有总帧数上限的短段空输入，按真实推进帧和新状态判断，不能用这一次拒绝作为阻塞依据。
<!-- /tool:stop_run -->

<!-- tool:finish_goal -->
## {tool:finish_goal}：提交达成报告

本次试玩进入过关卡后，用户交代的整次任务已经达成、且主控没有设置自动停止条件时，提交实际结果和依据。在 goal 填写当前登记的目标原文，或仅在聊天里交代的用户原任务。主控接受后正常收尾。整章任务按主控反馈完成结算核验。已有自动停止条件时等待主控核验。
<!-- /tool:finish_goal -->

<!-- tool:exit_level -->
## {tool:exit_level}：按要求返回地图

依据用户要求及当前权限离开关卡；返回地图不等于通关，也不等于结束整次任务。
<!-- /tool:exit_level -->

<!-- tool:cutscene -->
## {tool:cutscene}：剧情与可见选项

先读当前 story 和选项。已出现明信片、对白或选择节点时，执行相应的 advance 或 choose。主控对已核实的入场和已识别的演出有界等待。死亡或重试后可自行选择 act 确认/空输入，或 observe / watch 有界等待；普通死亡的确认可缩短部分动画，自动等待不是必经步骤。60 帧约 1 秒，动画或转场经过一段仍未结束很常见。若有过渡依据、试玩已恢复且没有待处理输入，可按反馈用 act 显式推进短段空输入，设置停止事件和总推进上限，逐段核对帧号与状态。

{story_reference_rule}
<!-- /tool:cutscene -->

<!-- tool:simulate -->
## {tool:simulate}：有限预测

只预测、不执行游戏；只能在本次额度和允许范围内使用，当前预算以工具反馈为准。不因此获得预置模拟器文件访问权。
<!-- /tool:simulate -->

<!-- tool:travel -->
## {tool:travel}：获准的计划外地图

仅用于主控已开放的绕行范围；不改变原计划目标，按当前权限及工具返回核对目的地与结果。
<!-- /tool:travel -->

<!-- tool:files -->
## {file_tool_names}：工作区文件

专用读取用于准备阶段的已读计量；搜索或程序读取不代替完整读取。/kb 只读，自己的笔记和程序保存在 /workspace。编辑不会操作游戏。

{files_reference_rule}
<!-- /tool:files -->

<!-- tool:programs -->
## {tool:Bash}：程序与辅助文件

程序在 /workspace 的断网容器内运行，受当前阶段、权限和额度限制。

需要生成碰撞矩阵时使用 /workspace/game/collision_matrix.py；展示进度或已有轨迹使用 /workspace/game/show.py；这些文件不替你执行游戏。
{program_reference_rule}
<!-- /tool:programs -->
