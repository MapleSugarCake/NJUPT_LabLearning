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
    def __init__(self, ID, name):
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

    urls_1 = ["1834023741387149313","1834023916826497026","1834024290224410625","1811954032592510977","1811954369810358273",
              "1811954561884315650","1811955013145288706","1811955307090501634","1811955432076566529","1811955598116478977",
              "1811955769445408769","1811955963645878274" ]
    urls_2 = ["1825800731916214274","1825800968487542785","1811956223315238913","1811956976448659457","1811958145694785538",
              "1811958275193921538","1811958440843763713","1811962125846011906","1811963385324199937","1811963788984016898",
              "1811964969227608065","1811965137477918721","1811965287961157634","1811966900587159554","1811967126412681217",
              "1811967455128674305","1811967653565390849","1811967826320384001","1811968026720034818","1811968411023138818"]
    urls_3 = ["1822840773138358273","1811968630964051970","1811968750166171649","1811968910526996482","1811969492239212546",
              "1811969705469239297","1811969834079182850","1811969984654696449","1811970267044601857","1811970407922884610",
              "1811970674085027842","1811970828338946050","1811970960912506881","1811971079925882881","1811971240819384322"]
    urls_4_B=["1822876505978638338","1811971453739032578","1811971579874336769","1814220927052005378","1811971695377080322",
              "1811971819893383170","1811971950877302786"]
    urls_4_A=["1811972108239200257","1811972260031062017","1811972376653684737","1811972677284618241","1811972845040001025",
              "1811972961406771201","1811973100049489921","1811973527671365634","1811973672563597313","1811973811512500225",
              "1811973964176777218","1811974098402893825","1811974262446317570"]
    urls_5=["1825801222351986690","1811974420634492930","1811974573638508545","1811974740739579906","1811974907039539201",
            "1811975014082371586","1811975239333273602","1811975357281296385","1811975515930845185","1811975677512212481"]
    urls_6=["1823157138953015298","1823157350878613505","1811975874812272642","1811976015459868674","1811976142853464066",
            "1811976264878350337","1811976459548581889","1811976597591515138","1811976741594554370","1811976866886803458",
            "1811977016988360706","1811977145652830209","1811977315916406785","1811977496376336385","1811977868973137922",
            "1811977667571048450","1811978140734676994","1811978271735373825","1811978434688278529","1811978579408543746",
            "1811978840201977858","1811979026353577986","1811979309171302402"]
    urls_7=["1825801464346550273","1811979845585035265","1811980006163963906","1811980172287762434"]
    urls_8=["1811980544502882306","1811980664334147586","1811980937106513921","1811981111396622337","1811983949870882817",
            "1811999378928525313","1811999512064122881","1811999629810819074","1811999768570978305"]
    urls_9=["1822878081573130241","1811999944693997570","1812000090966155266","1812000296390582273","1812000478884749314",
            "1812000649311903745","1812000793981837313","1812000952769798146","1812001115332632578","1812001289018761218",
            "1812001462532923393","1812001596553519106","1812001691441258498"]
    urls_10 = ["1823160098319699970","1812001903719178241","1812002182158049282","1812002376303992833","1812002589315915777",
               "1812002830790385666","1812003016094736386","1812003162782130177","1812003273268486146","1812003581604356098",
               "1812003717558525954","1812003845652570113","1812003961130147841","1812004167708008450","1812004352978804737",
               "1812004502467993602"]
    urls_11_C = ["1827980030299537410","1812004623993757697","1812004749810294786","1812005014382796802"]
    urls_11_A = ["1812005151045804034","1812005281945837569","1812005485759651841","1812006093640130561","1812006228558307329",
                 "1812006364332122114","1812006705882685442","1812006850334515201","1812007175351132161"]

    # ========== 新增：额外课程ID（所有学院均需执行） ==========
    extra_ids = [
        "2069661595847548929",
        "2069662760035344385",
        "2069663090571665409",
        "2069664135821582337",
        "2069664287567306754",
        "2047246967280680962"
    ]

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