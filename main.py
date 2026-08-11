#script by MapleCake NJUPT2025管院新生
#版本号v1.0.0

import requests
import json
import submodules

#多线程并发提高速度

#发送完成课程信息
def finish_class(id:int)->None:
    json_data = {'id': id}
    solve_question(id)
    s.post(SUBMIT_URL,headers=HEADERS,json=json_data)


def get_type()->list:
    data=s.get(COURSE_TYPE_URL, headers=HEADERS, params={"_t":s.cookies.get("vpn_timestamp"), "enlink-vpn":None})
    dict = json.loads(data.text)
    course_type_list = [item['id'] for item in dict['result']]
    return course_type_list

def get_course(course_type:int)->tuple[list,list]:
    resp = s.get(COURSE_URL, headers=HEADERS)
    # 将 JSON 字符串解析为 Python 字典
    data = json.loads(resp.text)

    course_id_list =[]
    course_name_list = []

    # 遍历 result 数组中的每一个对象
    for item in data["result"]:
        # 使用 get 方法可以防止字段不存在时报错（若不存在则返回 '未知'）
        item_id = item.get("id", "未知")
        item_name = item.get("type_dictText", "未知")
        item_finish = item.get("isFinish", "未知")
        if item_finish != "1":
            course_id_list.append(item_id)
            course_name_list.append(item_name)
    print(course_name_list)
    return course_id_list, course_name_list

def get_answer(id:int)->tuple[list,list]:
    resp = s.get(ANSWER_URL, headers=HEADERS,params={
        "_t":s.cookies.get("vpn_timestamp"),
        "id":id,
        "enlink-vpn":None,
    })
    data = json.loads(resp.text)
    questionid_list = []
    correctanswer_list = []
    # 2. 遍历 result 列表中的每一个题目对象
    for item in data["result"]:
        # 使用 get 方法获取，防止某个题目缺失该字段导致程序报错
        question_id = item.get("questionId")
        correct_answer = item.get("correctAnswer")
        questionid_list.append(question_id)
        correctanswer_list.append(correct_answer)
    return questionid_list, correctanswer_list

def solve_question(id):
    questionid_list,correctanswer_list=get_answer(id)
    for i,questionid in enumerate(questionid_list):
        json_data = {'id': id,
                     'option' : correctanswer_list[i],
                     'questionId' : questionid}
        s.post(SUBMIT_ANSWER_URL,headers=HEADERS,json=json_data)

def main()->int:

    course_type_list = get_type()

    for course_type_id in course_type_list:
        course_id_list, course_name_list = get_course(course_type_id)
        for course_id,course_name in zip(course_id_list,course_name_list):
            print(f"正在处理课程：{course_name}")
            finish_class(course_id)

    return 0


#全局变量
STATUS_CODE=0
HEADERS = {
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
}

#内网所需业务逻辑的URL
COURSE_TYPE_URL= "https://vpn.njupt.edu.cn:8443/http/webvpnc01f87dbae47c6e4069a3da910c73ebdc0a307b03b8b6cbdba61b1f29c7dbb41/jeecg-boot/jcedutec/courseSource/myCourseTypeList"
COURSE_URL="https://vpn.njupt.edu.cn:8443/http/webvpnc01f87dbae47c6e4069a3da910c73ebdc0a307b03b8b6cbdba61b1f29c7dbb41/jeecg-boot/jcedutec/courseSource/myCourseList"
SUBMIT_URL="https://vpn.njupt.edu.cn:8443/http/webvpnc01f87dbae47c6e4069a3da910c73ebdc0a307b03b8b6cbdba61b1f29c7dbb41/jeecg-boot/jcedutec/courseSource/finish?enlink-vpn"

ANSWER_URL="https://vpn.njupt.edu.cn:8443/http/webvpnc01f87dbae47c6e4069a3da910c73ebdc0a307b03b8b6cbdba61b1f29c7dbb41/jeecg-boot/jcedutec/courseSource/queryCourseQuestionRelaByMainId"
SUBMIT_ANSWER_URL="https://vpn.njupt.edu.cn:8443/http/webvpnc01f87dbae47c6e4069a3da910c73ebdc0a307b03b8b6cbdba61b1f29c7dbb41/jeecg-boot/jcedutec/courseSource/submitAnswer?enlink-vpn"

if __name__ == "__main__":
    print("script by MapleCake NJUPT2025管院新生")
    print("    本脚本坚持免费，如若您购买获得运行本脚本，作为一名光荣的南邮学子，请抵制倒买倒卖行为")
    print("    喵~管用贴吧给个好评喵~")
    print("    github给个小星星就最好啦:https://github.com/MapleSugarCake/LabLearningAutoPass")
    print("本脚本尚不完善，本脚本只可完成课程学习和课程答题，考试仍需自行手动完成。")
    print("若脚本报错，请再白天换个时间重试，夜晚你邮服务器会宕机")

    USERNAME = input("请输入学号账号：")
    PASSWORD = input("请输入密码：")
    E_Username = submodules.encrypt(USERNAME)
    E_Password = submodules.encrypt(PASSWORD)
    TOKEN,s = submodules.get_token(E_Username, E_Password)
    HEADERS["x-access-token"] = TOKEN

    main()
    print(f"执行结果代码(0为正常)：{STATUS_CODE}")
    print("程序执行完毕，请在网页上查看完成情况。按回车键退出...")
    input()