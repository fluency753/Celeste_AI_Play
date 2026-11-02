# 剧情与任务边界

当前为 agent 剧情模式。移动、暂停、菜单、重试、对白选择、结算和换章均由你显式决定；主控核验状态，不隐式代按或传送。先读 story 中的当前内容与选项，再选择行动。

complete 事件或 completed=true 只表示登记了成绩，不代表任务结束。你须自行处理确认和结算，回到稳定的选章地图（scene=Overworld、menu.current=OuiChapterSelect、menu.next 为空），主控才确认整章完成；还有计划目标时按新目标自行导航。
中途检查点目标抵达即可完成，无需为此退出关卡。计划结束是否继续活动，遵循本次停止条件；以主控的任务完成／结束报告为准。

{story_reference_rule}
