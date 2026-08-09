#script by MapleCake NJUPT2025管院新生
#版本号v0.43
import threading
import requests
import json
import time

#全局变量
success=0

#多线程并发提高速度
class myThread(threading.Thread):
    def __init__(self, ID, name:str):
        threading.Thread.__init__(self)
        self.ID = ID
        self.name = name
    def run(self):
            print(f"【线程开始】{self.name}")
            response = finish_class(self.ID)
            time.sleep(0.5)
            get_question(self.ID)
            if response==200:
                global success
                success+=1
            print("【线程结束】", self.name)

#发送完成课程信息
def finish_class(id):
    headers = {
        'Origin': 'http://10.22.192.38:9092',
        'Pragma': 'no-cache',
        'Referer': 'http://10.22.192.38:9092/',
        'content-type': 'application/json;charset=UTF-8',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
        'X-Access-Token': token
    }
    json_data = {'id': id}
    responses = requests.post(url='http://10.22.192.38:9090/jeecg-boot/jcedutec/courseSource/finish',headers=headers,json=json_data)
    print(responses.text)
    return responses.status_code

class questinAnswer:
    def __init__(self, questionID , videoID , options) -> None:
        self.questionID = questionID
        self.videoID = videoID
        self.options = options

def get_question(id):
    headers = {
        'Referer': 'http://10.22.192.38:9092/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
        'X-Access-Token': token
    }
    url=f'http://10.22.192.38:9090/jeecg-boot/jcedutec/courseSource/queryCourseQuestionRelaByMainId?id={id}'
    res = []
    resp = requests.get(url, headers=headers)
    print(resp.text)
    received = json.loads(resp.content.decode('utf-8'))
    questionList = received['result']
    if questionList:
        for question in questionList:
            res.append(questinAnswer(question['id'], question['courseId'],
                                     question['correctAnswer'] if len(question['correctAnswer']) == 1 else question[
                                         'correctAnswer'].split(',')))
    print(f"课程{id}问题获取成功")
    for a in res:
        solve_question(a)

def solve_question(ans):
    headers = {
        "Referer": "http://10.22.192.38:9092",
        'X-Access-Token': token
    }
    json_data = {'id': ans.videoID,
                 'option' : ans.options,
                 'questionId' : ans.questionID}
    responses=requests.post(url="http://10.22.192.38:9090/jeecg-boot/jcedutec/courseSource/submitAnswer",headers=headers,json=json_data)
    print(responses.status_code)
    if responses.status_code == 200:
        print(responses.text)

def main():

    types=int(input("管院等C类[5章节课程]同学请输入0，\n通信学院等B类[7章节课程]同学请输入1,\n材料学院等A类[11章节课程]同学请输入2:"))
    print(types)

    

    url_names = {
        id(urls_1): "第一章节",
        id(urls_2): "第二章节",
        id(urls_3): "第三章节",
        id(urls_4_B): "第四章节A【前7课】",
        id(urls_4_A): "第四章节B【后13课】",
        id(urls_5):"第五章节",
        id(urls_6):"第六章节",
        id(urls_7): "第七章节",
        id(urls_8):"第八章节",
        id(urls_9):"第九章节",
        id(urls_10): "第十章节",
        id(urls_11_C): "第十一章节A【前4课】",
        id(urls_11_A):"第十一章节B【后9课】",
        id(extra_ids): "额外章节"   # 新增映射
    }
    # 管院1,2,3,10,11C
    # 通院123,4B,7,10,11C
    #材料院123,4B,4A,56789,10,11C,11A
    match types:
        case 0:
            print("管院等C类[5章节课程]已加载")
            urls_lists = [urls_1, urls_2, urls_3, urls_10, urls_11_C, extra_ids]
        case 1:
            print("通信学院等B类[7章节课程]已加载")
            urls_lists = [urls_1, urls_2, urls_3, urls_4_B, urls_7, urls_10, urls_11_C, extra_ids]
        case 2:
            print("材料学院等A类[11章节课程]已加载")
            urls_lists = [urls_1, urls_2, urls_3, urls_4_B, urls_4_A,urls_5,urls_6,urls_7, urls_8,urls_9,urls_10, urls_11_C,urls_11_A, extra_ids]
        case _:
            print("输入错误。")
            exit()
    threads=[]
    for a in urls_lists:
        global success
        success=0

        for i in range(len(a)):
            thread = myThread(a[i], chr(65 + i))  # chr(65) 是 'A'
            threads.append(thread)
            time.sleep(0.5)
            thread.start()
        for i in threads:
            i.join()

        list_name = url_names.get(id(a), "未知章节")

        if success==len(a):
            print("\n")
            print(f"{list_name}已完成学习")
            print("\n")
        else:
            print("\n")
            print(f"{list_name}存在 {len(a)-success}个 学习失败的章节")
            print("\n")

if __name__ == "__main__":
    print("script by MapleCake NJUPT2025管院新生")
    print("    本脚本坚持免费，如若您购买获得运行本脚本，作为一名光荣的南邮学子，请抵制倒买倒卖行为")
    print("    喵~管用贴吧给个好评喵~")
    print("    github给个小星星就最好啦:https://github.com/MapleSugarCake/LabLearningAutoPass")
    print("本脚本尚不完善，本脚本只可完成课程学习和课程答题，考试仍需自行手动完成。")
    print("若脚本报错，请再白天换个时间重试，夜晚你邮服务器会宕机")
    print("现在需要您按以下操作帮助登录:")
    print("    1.请校内同学从http://10.22.192.38:9092/登录自己的用户。""\n"
          "      ###校外同学无法使用该脚本###")
    print("    2.任意选择一个课程打开")
    print("    3.Chrome/Edge浏览器用户请右键选择检查后，再选择网络")
    print("    4.在页面左上角的过滤栏里输入updatevisits（是左上角，不是下面的过滤框）")
    print("    5.刷新页面，现在你可以看到网页多了两个包")
    print("    6.选择类型为xhr的包，在请求标头里复制x-access-token的数据，并粘贴在本程序中")
    token = input("请输入你的X-Access-Token：")
    main()
    print("程序执行完毕，请在网页上查看完成情况。按回车键退出...")
    input()