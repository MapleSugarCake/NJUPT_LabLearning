"""作为源码脚本和 Windows 可执行文件的启动入口。

直接启动时输出当前脚本横幅，并调用导入的 labpass_cli.cli.entrypoint。
entrypoint 负责运行交互流程、处理可执行文件暂停并发出进程退出状态。
本文件不定义登录、课程处理或参数解析逻辑。

本文件定义：
    无自行定义的函数、类或模块变量。
"""

from labpass_cli.cli import entrypoint

if __name__ == "__main__":
    print("****************************************************************")
    print("script by NJUPT2025 MapleCake")
    print("本脚本坚持免费，请抵制倒买倒卖行为")
    print("github地址:https://github.com/MapleSugarCake/LabLearningAutoPass")
    print(
        "本脚本尚不完善，本脚本只可完成课程学习和课程答题，考试仍需自行手动完成，就当对知识的检验吧~"
    )
    print("若脚本报错，请换个时间重试")
    print("****************************************************************")
    entrypoint()
