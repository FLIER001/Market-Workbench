export interface SectorCompany {
  code: string;
  name: string;
}

export interface SectorNode {
  stage?: "上游" | "中游" | "下游";
  name: string;
  description: string;
  companies: SectorCompany[];
}

export interface SectorEvent {
  date: string;
  status: "已官宣" | "基于事实推断";
  title: string;
  basis: string;
  judgment: string;
  direction: "positive" | "negative" | "mixed";
  confidence: "高" | "中" | "低";
  source: string;
  url: string;
}

export interface SectorWatchpoint {
  direction: "positive" | "negative";
  title: string;
  detail: string;
  window: string;
}

export interface SectorPolicyEvidence {
  title: string;
  source: string;
  date: string;
  url: string;
  direction: "positive" | "negative" | "mixed";
}

export interface SectorResearch {
  asOf: string;
  radarKeys: string[];
  newsKeywords: string[];
  nodes: SectorNode[];
  watchpoints: SectorWatchpoint[];
  policy: {
    score: -2 | -1 | 0 | 1 | 2;
    label: string;
    confidence: "高" | "中" | "低";
    summary: string;
    evidence: SectorPolicyEvidence[];
  };
}

// 人工维护的事实快照；只随代码版本发布更新，不参与任何定时任务或自动 AI 改写。
export const SECTOR_RESEARCH_VERSION = "2026-08-01.manual";

const FYP_URL = "https://www.ndrc.gov.cn/fggz/fzzlgh/gjfzgh/202603/U020260317369114704096.pdf";
const HUMANOID_ACTION_URL = "https://www.miit.gov.cn/jgsj/kjs/wjfb/art/2026/art_cd666691abf8471fb8553d463aa416e3.html";
const HUMANOID_TRAINING_URL = "https://www.miit.gov.cn/zwgk/zcwj/wjfb/tz/art/2026/art_f291ccd3da4c47ce95741de63cc088e6.html";
const LOW_ALTITUDE_INFRA_URL = "https://www.miit.gov.cn/zwgk/zcwj/wjfb/yj/art/2026/art_d1cb1667897e4c999a303d110b6691dc.html";
const STORAGE_REPORT_URL = "https://www.nea.gov.cn/20260730/0a1e9823b7a14e8ba16e82c34b3bea2b/c.html";
const ENERGY_PRESS_URL = "https://www.nea.gov.cn/20260730/3ce671c387574eeeb120fc3825be0399/c.html";
const GAME_APPROVAL_URL = "https://www.nppa.gov.cn/bsfw/jggs/yxspjg/index.html";
const GAME_RULE_URL = "https://www.gov.cn/zhengce/zhengceku/2021-09/01/content_5634661.htm";

const co = (code: string, name: string): SectorCompany => ({ code, name });
const node = (name: string, description: string, companies: SectorCompany[]): SectorNode => ({
  name,
  description,
  companies,
});
const pos = (title: string, detail: string, window = "未来 6—12 个月"): SectorWatchpoint => ({
  direction: "positive",
  title,
  detail,
  window,
});
const neg = (title: string, detail: string, window = "持续观察"): SectorWatchpoint => ({
  direction: "negative",
  title,
  detail,
  window,
});
const fypEvidence = (title: string): SectorPolicyEvidence => ({
  title,
  source: "国家发展改革委｜“十五五”规划纲要",
  date: "2026-03",
  url: FYP_URL,
  direction: "positive",
});

const baseSectorResearch: Record<string, SectorResearch> = {
  humanoid: {
    asOf: "2026-08-01",
    radarKeys: ["robot", "ai"],
    newsKeywords: ["humanoid", "robot", "具身", "人形机器人", "embodied"],
    nodes: [
      node("精密传动", "减速器与丝杠把电机输出转化为关节和直线运动，是成本、寿命和量产良率的关键。", [
        co("688017", "绿的谐波"), co("002472", "双环传动"), co("603009", "北特科技"), co("300580", "贝斯特"),
      ]),
      node("电机与驱动", "无框力矩电机、空心杯电机及伺服驱动决定功率密度和控制精度。", [
        co("603728", "鸣志电器"), co("300124", "汇川技术"),
      ]),
      node("灵巧手与传感", "末端执行器、六维力与触觉传感决定精细操作能力和人机协作安全。", [
        co("300115", "长盈精密"), co("603662", "柯力传感"),
      ]),
      node("本体与系统集成", "整机厂负责结构、运动控制、模型和场景工程的系统级协同。", [
        co("300024", "机器人"), co("002747", "埃斯顿"),
      ]),
    ],
    watchpoints: [
      pos("实景实训与规模部署", "跟踪省级地区和央企的场景台账、作业成功率、效率、安全及经济性报告；11 月 30 日前为阶段总结窗口。", "截至 2026-11-30"),
      pos("头部整机定点与量产", "车企、3C、物流等真实订单由样机转为持续采购。"),
      neg("量产良率与成本不达预期", "丝杠、减速器、灵巧手寿命或一致性无法满足工业场景。"),
      neg("安全事故或标准收紧", "人机混行事故可能推迟部署，并抬高认证和保险成本。"),
    ],
    policy: {
      score: 2,
      label: "强支持",
      confidence: "高",
      summary: "中央规划与两部门专项行动已从技术攻关推进到真实场景验证、常态部署和按效用付费探索；下一步应以验证报告及持续采购而非样机数量判断兑现。",
      evidence: [
        {
          title: "2026年度人形机器人与具身智能实景实训专项行动",
          source: "工业和信息化部、国务院国资委",
          date: "2026",
          url: HUMANOID_ACTION_URL,
          direction: "positive",
        },
        fypEvidence("具身智能列入未来产业，支持场景开放与规模化发展"),
      ],
    },
  },
  "ai-computing": {
    asOf: "2026-08-01",
    radarKeys: ["ai", "semi"],
    newsKeywords: ["AI chip", "GPU", "data center", "算力", "智算", "服务器"],
    nodes: [
      node("AI 芯片", "训练和推理芯片决定算力供给上限，重点观察国产生态、软件栈与客户验证。", [
        co("688256", "寒武纪"), co("688041", "海光信息"),
      ]),
      node("高速光互连", "光模块与光器件承担集群内外高带宽连接，速率代际和良率决定价值量。", [
        co("300308", "中际旭创"), co("300502", "新易盛"), co("002281", "光迅科技"),
      ]),
      node("封装与 PCB", "先进封装、高多层 PCB 连接芯片、存储和交换网络，是扩容时的同步瓶颈。", [
        co("002156", "通富微电"), co("600584", "长电科技"), co("002463", "沪电股份"), co("300476", "胜宏科技"),
      ]),
      node("供电与液冷", "高功率机柜把电力、配电和液冷推向更高价值量与更严格可靠性要求。", [
        co("002837", "英维克"), co("300499", "高澜股份"),
      ]),
    ],
    watchpoints: [
      pos("算力资本开支上修", "云厂商、运营商或互联网平台提高智算集群建设预算。"),
      pos("国产芯片与软件栈验证", "大规模集群稳定运行、客户迁移工具成熟或政府算力采购落地。"),
      neg("海外管制升级", "高端芯片、设备、互连和 EDA 获取受限，影响扩容节奏。"),
      neg("利用率和回报率走弱", "建成算力空置、模型成本下降快于需求增长，压制新增资本开支。"),
    ],
    policy: {
      score: 2, label: "强支持", confidence: "高",
      summary: "国家规划明确加强高性能智算供给、全国一体化调度与自主可控软硬件生态。",
      evidence: [fypEvidence("强化算力算法数据供给，建设高性能高质量智算资源")],
    },
  },
  hbm: {
    asOf: "2026-08-01",
    radarKeys: ["semi", "ai"],
    newsKeywords: ["HBM", "high bandwidth memory", "memory", "高带宽存储"],
    nodes: [
      node("存储与接口芯片", "关注存储产品迭代、接口设计能力和高端产品客户验证。", [
        co("688525", "佰维存储"), co("603986", "兆易创新"),
      ]),
      node("先进封装", "堆叠、键合与封测良率决定 HBM 的带宽、功耗和量产成本。", [
        co("002156", "通富微电"), co("600584", "长电科技"),
      ]),
      node("制造设备", "CMP、薄膜沉积等设备贯穿晶圆减薄、互连和堆叠前工序。", [
        co("688120", "华海清科"), co("688072", "拓荆科技"),
      ]),
      node("关键材料", "靶材与抛光材料影响先进制程和封装的一致性、洁净度与良率。", [
        co("300666", "江丰电子"), co("688019", "安集科技"),
      ]),
    ],
    watchpoints: [
      pos("海外大厂扩产与认证", "新一代 HBM 量产、封装设备扩单或国产材料通过客户验证。"),
      pos("国产高带宽存储突破", "接口、堆叠或封装环节出现可核验的产品和客户进展。"),
      neg("供给快速释放", "扩产集中投放导致价格和设备订单增速回落。"),
      neg("技术路线迭代失败", "良率、散热或功耗不达标造成认证延期。"),
    ],
    policy: {
      score: 2, label: "强支持", confidence: "中",
      summary: "政策直接支持高性能 AI 芯片与自主可控生态，HBM 受益路径明确，但专项细则仍需逐项核实。",
      evidence: [fypEvidence("研制高性能人工智能芯片并完善自主可控软硬件生态")],
    },
  },
  cpo: {
    asOf: "2026-08-01",
    radarKeys: ["semi", "ai"],
    newsKeywords: ["CPO", "optical interconnect", "silicon photonics", "光模块", "硅光"],
    nodes: [
      node("高速光模块", "800G/1.6T 等速率升级直接抬升器件性能、功耗和制造要求。", [
        co("300308", "中际旭创"), co("300502", "新易盛"),
      ]),
      node("光芯片与器件", "激光器、调制器和无源器件是性能与国产化率的核心约束。", [
        co("688498", "源杰科技"), co("300620", "光库科技"),
      ]),
      node("光引擎与耦合", "精密耦合、封装和连接决定 CPO 量产良率与系统维护方式。", [
        co("300394", "天孚通信"), co("002281", "光迅科技"),
      ]),
      node("交换芯片", "交换容量与 SerDes 速率决定光互连升级节奏。", [
        co("688702", "盛科通信-U"), co("688041", "海光信息"),
      ]),
    ],
    watchpoints: [
      pos("1.6T 放量或 CPO 商用", "云厂商、交换机厂公开量产节奏和规模采购。"),
      pos("硅光/光芯片国产验证", "核心器件通过头部客户认证并形成持续订单。"),
      neg("技术路线后移", "可插拔模块功耗优化延长生命周期，CPO 导入慢于预期。"),
      neg("客户集中与价格压力", "单一客户砍单或代际切换引发库存和价格波动。"),
    ],
    policy: {
      score: 1, label: "支持", confidence: "中",
      summary: "政策通过智算基础设施和高性能芯片间接拉动高速互连，CPO 本身仍以产业需求驱动为主。",
      evidence: [fypEvidence("加强算力设施支撑并推动云边端协同")],
    },
  },
  semiconductor: {
    asOf: "2026-08-01",
    radarKeys: ["semi"],
    newsKeywords: ["semiconductor", "chip", "晶圆", "半导体", "EDA"],
    nodes: [
      node("设备", "刻蚀、薄膜、清洗等设备的工艺覆盖率和客户验证是国产替代的先行指标。", [
        co("002371", "北方华创"), co("688012", "中微公司"), co("688072", "拓荆科技"),
      ]),
      node("材料", "光刻、抛光、前驱体等材料验证周期长，进入产线后黏性较高。", [
        co("688019", "安集科技"), co("002409", "雅克科技"),
      ]),
      node("EDA 与设计工具", "工具链完整度影响先进芯片研发效率和生态自主性。", [
        co("301269", "华大九天"), co("688206", "概伦电子"),
      ]),
      node("晶圆制造与封测", "产能利用率、工艺节点和先进封装共同决定产业兑现。", [
        co("688981", "中芯国际"), co("688347", "华虹宏力"), co("600584", "长电科技"),
      ]),
    ],
    watchpoints: [
      pos("国产设备材料验证提速", "更多关键层进入量产线并形成重复订单。"),
      pos("先进工艺与封装扩产", "晶圆厂资本开支、产能利用率和先进封装订单同步改善。"),
      neg("外部限制扩大", "设备、软件、材料或代工限制升级，影响研发和交付。"),
      neg("成熟制程供给过剩", "价格竞争和利用率下降侵蚀制造端盈利。"),
    ],
    policy: {
      score: 2, label: "强支持", confidence: "高",
      summary: "高性能芯片和自主可控软硬件生态被列为数字基础设施核心任务。",
      evidence: [fypEvidence("研制高性能人工智能芯片，培育自主可控软硬件生态")],
    },
  },
  "solid-state-battery": {
    asOf: "2026-08-01",
    radarKeys: ["energy", "auto"],
    newsKeywords: ["solid-state battery", "固态电池", "sulfide electrolyte"],
    nodes: [
      node("正极与复合材料", "高镍、富锂和固固界面改性影响能量密度与循环寿命。", [
        co("300073", "当升科技"), co("688005", "容百科技"),
      ]),
      node("电解质与添加剂", "硫化物、氧化物、聚合物路线仍在竞争，核心看离子电导率和制造环境。", [
        co("002709", "天赐材料"), co("300037", "新宙邦"),
      ]),
      node("制造设备", "叠片、干法、等静压和激光工艺变化带来新设备需求。", [
        co("300450", "先导智能"), co("688518", "联赢激光"),
      ]),
      node("电芯与整车验证", "样品、装车、法规测试和量产良率决定商业化时间表。", [
        co("300750", "宁德时代"), co("002074", "国轩高科"),
      ]),
    ],
    watchpoints: [
      pos("中试线和装车验证", "头部电池厂披露可重复的能量密度、寿命、安全及成本数据。"),
      pos("材料路线定型", "硫化物或其他路线获得整车厂定点和规模采购。"),
      neg("量产时间表延期", "实验室性能无法复制到大面积电芯，或良率过低。"),
      neg("现有液态体系继续进步", "快充、硅碳和结构创新缩小固态电池的性能优势。"),
    ],
    policy: {
      score: 1, label: "支持", confidence: "中",
      summary: "国家能源局继续支持新型储能多技术路线并举，但当前仍由锂离子电池主导；固态路线的利好必须由中试良率、安全、寿命和成本数据验证。",
      evidence: [fypEvidence("推动新型太阳能电池、新型储能及新能源汽车关键技术创新")],
    },
  },
  "low-altitude": {
    asOf: "2026-08-01",
    radarKeys: ["space", "auto"],
    newsKeywords: ["eVTOL", "low altitude", "低空", "适航", "无人机"],
    nodes: [
      node("整机与运营", "eVTOL、通航和无人机运营的关键是适航、场景经济性与持续订单。", [
        co("002085", "万丰奥威"), co("600372", "中航机载"),
      ]),
      node("空管与通信导航", "低空航路需要通信、导航、监视、气象和协同空管系统。", [
        co("000801", "四川九洲"), co("688631", "莱斯信息"),
      ]),
      node("定位与链路", "高精度定位和抗干扰通信是规模化低空运行的基础能力。", [
        co("300627", "华测导航"), co("002465", "海格通信"),
      ]),
      node("航空核心部件", "飞控、机载系统和动力部件决定安全冗余与适航进度。", [
        co("000738", "航发控制"), co("600372", "中航机载"),
      ]),
    ],
    watchpoints: [
      pos("适航取证与商业航线", "型号合格证、生产许可证和常态化运营航线增加。"),
      pos("低空基础设施招标", "跟踪地方航路、起降点、5G/5G-A 通感、北斗增强与空管系统采购，以及 2027 年航路通信覆盖目标的实施进度。"),
      neg("安全事故与监管收紧", "事故可能导致空域、运营和适航规则阶段性收紧。"),
      neg("场景经济性不足", "客流、货运密度或运维成本无法支撑规模运营。"),
    ],
    policy: {
      score: 2, label: "强支持但强监管", confidence: "高",
      summary: "政策已从产业方向延伸到通信、感知、导航和智能网联系统建设，并提出 2027 年低空公共航路地面移动通信覆盖率不低于 90%；适航、空域与数据安全仍是硬约束。",
      evidence: [fypEvidence("推进低空经济健康有序发展，建设低空基础设施并强化安全保障")],
    },
  },
  "smart-driving": {
    asOf: "2026-08-01",
    radarKeys: ["auto", "ai"],
    newsKeywords: ["autonomous driving", "ADAS", "智能驾驶", "robotaxi"],
    nodes: [
      node("域控制器与计算平台", "硬件算力、软件中间件与整车适配共同决定量产份额。", [
        co("002920", "德赛西威"), co("688326", "经纬恒润-W"),
      ]),
      node("感知与算法", "摄像头、3D 视觉和融合算法影响复杂场景识别与安全冗余。", [
        co("688322", "奥比中光-W"), co("002230", "科大讯飞"),
      ]),
      node("线控底盘", "转向、制动和悬架执行器是高阶驾驶闭环控制的物理基础。", [
        co("601689", "拓普集团"), co("002050", "三花智控"),
      ]),
      node("整车与数据闭环", "车队规模、数据回传和 OTA 能力决定算法迭代速度。", [
        co("601127", "赛力斯"), co("002594", "比亚迪"),
      ]),
    ],
    watchpoints: [
      pos("准入扩大与高阶功能落地", "更多城市、车型和场景进入有条件自动驾驶运营。"),
      pos("芯片/算法降本", "中端车型渗透率提升，单车成本下降同时体验改善。"),
      neg("重大安全事故", "事故归责和功能宣传争议可能触发法规收紧。"),
      neg("价格战压缩价值量", "整车降价向供应链传导，域控和传感器单价承压。"),
    ],
    policy: {
      score: 1, label: "支持但审慎准入", confidence: "高",
      summary: "政策支持智能网联汽车关键技术，同时推进立法、测试准入和安全责任体系。",
      evidence: [fypEvidence("支持智能驾驶关键技术创新，并推进新兴领域立法")],
    },
  },
  "innovative-drug": {
    asOf: "2026-08-01",
    radarKeys: ["bio"],
    newsKeywords: ["drug", "biotech", "clinical", "创新药", "ADC"],
    nodes: [
      node("自主研发平台", "靶点发现、临床开发和全球注册能力决定管线质量与可持续性。", [
        co("600276", "恒瑞医药"), co("688235", "百济神州"),
      ]),
      node("ADC 与新技术平台", "差异化靶点、连接子、载荷和临床数据决定授权价值。", [
        co("688331", "荣昌生物"), co("688506", "百利天恒"),
      ]),
      node("CRO/CDMO", "研发投入和融资环境变化会传导到临床、生产和外包订单。", [
        co("603259", "药明康德"), co("300759", "康龙化成"),
      ]),
      node("商业化与出海", "医保、院内准入、海外授权和自主销售共同决定收入兑现。", [
        co("600196", "复星医药"), co("000963", "华东医药"),
      ]),
    ],
    watchpoints: [
      pos("关键临床数据与海外授权", "核心管线达到主要终点、获批或达成高质量 BD。"),
      pos("支付与院内准入改善", "医保谈判、商保和临床使用支持扩大真实患者覆盖。"),
      neg("临床失败或安全性信号", "主要终点未达成、黑框警告或试验暂停造成估值重定价。"),
      neg("海外合规与供应链限制", "生物安全、数据或跨境合作规则影响订单和授权。"),
    ],
    policy: {
      score: 2, label: "强支持", confidence: "高",
      summary: "规划明确支持创新药临床使用和生物医药产业发展，但审评、支付和合规约束仍然重要。",
      evidence: [fypEvidence("加快生物医药发展并支持创新药临床使用")],
    },
  },
  "power-grid": {
    asOf: "2026-08-01",
    radarKeys: ["energy"],
    newsKeywords: ["grid", "power transmission", "电网", "特高压", "输变电"],
    nodes: [
      node("一次设备", "开关、GIS 和变压器直接受益于主网扩建与更新周期。", [
        co("600312", "平高电气"), co("600089", "特变电工"), co("600550", "保变电气"),
      ]),
      node("二次设备与调度", "保护、自动化和调度系统决定新能源高比例接入后的稳定运行。", [
        co("600406", "国电南瑞"), co("002028", "思源电气"),
      ]),
      node("线缆与海缆", "跨区输电、海上风电和配网改造拉动高压线缆与海缆需求。", [
        co("600522", "中天科技"), co("600487", "亨通光电"),
      ]),
      node("新能源并网", "逆变、储能和电能质量设备平衡波动性电源。", [
        co("300274", "阳光电源"), co("300693", "盛弘股份"),
      ]),
    ],
    watchpoints: [
      pos("电网投资和特高压核准", "2026 年上半年电网投资同比增长 13.5%，继续跟踪国家电网、南方电网资本开支、特高压核准与主设备招标能否连续兑现。"),
      pos("配网数字化和新能源并网", "分布式能源、充电设施和数据中心推动配网扩容。"),
      neg("项目核准或交付延期", "地方配套、原材料和施工进度拖慢收入确认。"),
      neg("铜铝等成本上涨", "原材料涨价而合同调价机制滞后，压缩设备利润。"),
    ],
    policy: {
      score: 2, label: "强支持", confidence: "高",
      summary: "2026 年上半年电网投资同比增长 13.5%，高温负荷和新能源占比上升继续强化跨区输电、配网扩容与调度需求；设备端兑现仍看核准、招标和交付。",
      evidence: [fypEvidence("推动能源体系转型与新型电力系统建设")],
    },
  },
  defense: {
    asOf: "2026-08-01",
    radarKeys: ["space"],
    newsKeywords: ["defense", "aerospace", "军工", "航空装备", "舰船"],
    nodes: [
      node("航空整机", "订单、交付节奏和维修保障决定航空装备产业链兑现。", [
        co("600760", "中航沈飞"), co("000768", "中航西飞"),
      ]),
      node("航空发动机", "发动机研制、批产和维修周期长，核心看交付与可靠性。", [
        co("600893", "航发动力"), co("000738", "航发控制"),
      ]),
      node("舰船装备", "船舶设计、建造和总装受订单与产能利用率驱动。", [
        co("600150", "中国船舶"), co("601989", "中国重工"),
      ]),
      node("电子与连接", "航电、雷达和高可靠连接器覆盖多型装备，平台化能力更关键。", [
        co("600879", "航天电子"), co("002179", "中航光电"),
      ]),
    ],
    watchpoints: [
      pos("订单与交付节奏改善", "合同负债、存货转化和批产交付出现连续改善。"),
      pos("资产整合或产能释放", "专业化整合、重大资产安排和新产线达产。"),
      neg("项目验收延期", "研制、定型或验收周期拉长，影响收入确认。"),
      neg("信息披露有限", "订单和进度透明度低，市场预期容易偏离实际兑现。"),
    ],
    policy: {
      score: 1, label: "支持，公开信息有限", confidence: "中",
      summary: "高端装备和航空航天属于战略性产业，但具体项目、订单和节奏需以公司公告为准。",
      evidence: [fypEvidence("发展高端装备与航空航天战略性新兴产业")],
    },
  },
  fusion: {
    asOf: "2026-08-01",
    radarKeys: ["science", "energy"],
    newsKeywords: ["fusion", "tokamak", "核聚变", "托卡马克"],
    nodes: [
      node("超导磁体", "高场磁体和低温超导材料决定装置尺寸、约束能力和运行成本。", [
        co("688122", "西部超导"), co("600105", "永鼎股份"),
      ]),
      node("真空室与主机结构", "大型真空容器、精密焊接和成形能力决定工程建造质量。", [
        co("300092", "科新机电"), co("603011", "合锻智能"),
      ]),
      node("加热与电源系统", "微波、射频、脉冲电源和控制系统支撑等离子体启动和稳态运行。", [
        co("688776", "国光电气"), co("002028", "思源电气"),
      ]),
      node("第一壁与特种材料", "钨、铜合金和耐辐照材料面对高热流与中子损伤。", [
        co("000969", "安泰科技"), co("002318", "久立特材"),
      ]),
    ],
    watchpoints: [
      pos("重大装置招标与里程碑", "实验装置新建、升级、点火或稳态运行指标取得可核验进展。"),
      pos("工程化路线融资", "示范堆、关键部件产线和社会资本投入增加。"),
      neg("技术里程碑延期", "净能量、材料寿命或稳态运行目标未达预期。"),
      neg("商业化周期过长", "订单以科研装置为主，短期收入与主题热度不匹配。"),
    ],
    policy: {
      score: 2, label: "强支持，产业早期", confidence: "高",
      summary: "核聚变能被列为未来产业，政策支持明确，但商业化兑现周期和技术不确定性都很高。",
      evidence: [fypEvidence("推动核聚变能成为新的经济增长点")],
    },
  },
  "business-space": {
    asOf: "2026-08-01",
    radarKeys: ["space"],
    newsKeywords: ["launch", "satellite", "commercial space", "商业航天", "卫星互联网"],
    nodes: [
      node("运载火箭", "发射频次、复用能力和单位成本决定商业发射供给。", [
        co("003009", "中天火箭"), co("600879", "航天电子"),
      ]),
      node("卫星制造", "批量化设计、载荷和总装测试决定星座建设速度。", [
        co("600118", "中国卫星"), co("688568", "中科星图"),
      ]),
      node("元器件与连接", "高可靠连接、继电器和电子元件覆盖火箭与卫星平台。", [
        co("002025", "航天电器"), co("002179", "中航光电"),
      ]),
      node("运营与应用", "卫星通信、遥感和导航服务决定产业链最终商业回报。", [
        co("601698", "中国卫通"), co("300627", "华测导航"),
      ]),
    ],
    watchpoints: [
      pos("星座组网和发射提速", "招标、卫星批产与年度发射次数连续上修。"),
      pos("可复用火箭节点", "回收试验、发动机复用和商业订单出现实质进展。"),
      neg("发射失败或任务延期", "技术事故可能中断排期并抬升保险与质量成本。"),
      neg("商业模式不清晰", "星座建设投入大，但终端、带宽和应用收入不足。"),
    ],
    policy: {
      score: 2, label: "强支持", confidence: "高",
      summary: "航空航天和北斗应用获得规划支持，商业航天仍需关注发射许可、安全和频轨资源。",
      evidence: [fypEvidence("发展航空航天战略产业并加强北斗系统创新应用")],
    },
  },
  "ai-pharma": {
    asOf: "2026-08-01",
    radarKeys: ["bio", "ai", "science"],
    newsKeywords: ["AI drug", "biotech", "medical AI", "生物医药", "AI制药"],
    nodes: [
      node("药物研发与转化", "临床管线质量比模型数量更重要，需验证 AI 是否缩短周期或提高成功率。", [
        co("600276", "恒瑞医药"), co("000661", "长春高新"),
      ]),
      node("临床前模型", "模型动物和实验平台支撑靶点验证与药效安全评价。", [
        co("688046", "药康生物"), co("300759", "康龙化成"),
      ]),
      node("医疗设备与影像", "影像、检验和设备数据是医疗 AI 进入真实临床场景的入口。", [
        co("300760", "迈瑞医疗"), co("688271", "联影医疗"),
      ]),
      node("研发服务平台", "CRO 的数据、实验和临床执行能力帮助 AI 结果完成湿实验与注册验证。", [
        co("603259", "药明康德"), co("300759", "康龙化成"),
      ]),
    ],
    watchpoints: [
      pos("AI 发现管线进入临床", "由 AI 参与发现的项目获得积极临床数据或高质量授权。"),
      pos("医疗数据和场景开放", "合规数据空间、医院合作和医疗器械审批路径更清晰。"),
      neg("模型效果难以复现", "计算结果无法在湿实验或临床中提高成功率。"),
      neg("数据与伦理监管收紧", "患者隐私、跨境数据和算法责任规则提高应用成本。"),
    ],
    policy: {
      score: 2, label: "强支持但重合规", confidence: "高",
      summary: "人工智能与生物医药均为战略产业，医疗应用同时受数据、伦理和临床监管约束。",
      evidence: [fypEvidence("发展人工智能与生物医药，并支持创新药临床使用")],
    },
  },
  resources: {
    asOf: "2026-08-01",
    radarKeys: ["energy", "macro"],
    newsKeywords: ["rare earth", "critical minerals", "稀土", "锗", "铟", "钨"],
    nodes: [
      node("稀土", "配额、冶炼分离、磁材需求和出口规则共同影响价格与盈利。", [
        co("600111", "北方稀土"), co("000831", "中国稀土"),
      ]),
      node("锗与镓", "光通信、红外和半导体需求叠加资源与出口政策，价格弹性较高。", [
        co("600497", "驰宏锌锗"), co("002428", "云南锗业"), co("601600", "中国铝业"),
      ]),
      node("铟与锡", "显示、光伏和焊料需求决定伴生金属的真实消耗。", [
        co("000960", "锡业股份"), co("600549", "厦门钨业"),
      ]),
      node("钨与战略小金属", "矿端供给约束、深加工占比和高端制造需求决定利润分配。", [
        co("600549", "厦门钨业"), co("000657", "中钨高新"),
      ]),
    ],
    watchpoints: [
      pos("供给约束或战略收储", "配额、环保、出口许可或收储压缩现货供给。"),
      pos("高端需求放量", "新能源、军工、半导体和光通信带动深加工产品订单。"),
      neg("需求走弱与价格回落", "下游去库存或替代材料降低边际需求。"),
      neg("政策口径变化", "出口、配额或环保规则变化造成价格和出货量剧烈波动。"),
    ],
    policy: {
      score: 1, label: "资源安全导向", confidence: "中",
      summary: "政策重点是战略资源安全、规范开发与高端利用，未必等同于持续涨价。",
      evidence: [fypEvidence("强化产业链供应链韧性与关键资源安全保障")],
    },
  },
  "ai-application": {
    asOf: "2026-08-01",
    radarKeys: ["ai", "tech"],
    newsKeywords: ["AI agent", "enterprise AI", "大模型应用", "智能体", "Agent"],
    nodes: [
      node("办公与企业软件", "订阅付费、活跃用户和每席位收入比功能发布更能验证商业化。", [
        co("600588", "用友网络"), co("688111", "金山办公"),
      ]),
      node("模型与语音入口", "模型能力、推理成本和行业数据决定应用体验与毛利。", [
        co("002230", "科大讯飞"), co("300229", "拓尔思"),
      ]),
      node("内容与创意工具", "视频、图片和文档生成的留存、版权与海外付费是核心变量。", [
        co("300624", "万兴科技"), co("300315", "掌趣科技"),
      ]),
      node("工业智能", "控制系统、工业数据和现场闭环决定 AI 能否从辅助决策走向生产控制。", [
        co("688777", "中控技术"), co("300124", "汇川技术"),
      ]),
    ],
    watchpoints: [
      pos("付费和留存验证", "企业续费、席位扩张或 AI 产品收入单独披露。"),
      pos("模型成本继续下降", "推理价格下降而调用量、场景和毛利同步提升。"),
      neg("同质化与价格战", "基础模型能力趋同，应用缺乏数据和渠道壁垒。"),
      neg("版权与数据监管", "训练数据、生成内容责任和跨境合规提高成本。"),
    ],
    policy: {
      score: 2, label: "强支持，规则加速形成", confidence: "高",
      summary: "“人工智能+”与场景开放明确支持应用扩散，同时数据治理和安全要求同步提高。",
      evidence: [fypEvidence("深化拓展“人工智能+”，促进模型算法与实体经济融合")],
    },
  },
  "ai-hardware": {
    asOf: "2026-08-01",
    radarKeys: ["consumer", "ai", "semi"],
    newsKeywords: ["AI glasses", "edge AI", "AI PC", "AI手机", "AI眼镜"],
    nodes: [
      node("端侧芯片", "功耗、算力和工具链决定模型能否在终端稳定运行。", [
        co("603501", "豪威集团"), co("300223", "北京君正"),
      ]),
      node("光学与显示", "轻量化、亮度、波导效率和视觉感知决定 AI 眼镜体验。", [
        co("002273", "水晶光电"), co("000725", "京东方Ａ"),
      ]),
      node("精密制造", "结构件、连接和组装良率决定新品爬坡与供应链份额。", [
        co("002475", "立讯精密"), co("300433", "蓝思科技"),
      ]),
      node("声学与可穿戴", "麦克风、扬声器和整机设计构成自然交互入口。", [
        co("002241", "歌尔股份"), co("002475", "立讯精密"),
      ]),
    ],
    watchpoints: [
      pos("头部品牌发布与销量", "AI 手机、PC、眼镜新品功能和首销数据超预期。"),
      pos("端侧模型能力跃迁", "离线多模态、低功耗推理和续航出现实质改善。"),
      neg("新品需求不及预期", "功能缺乏刚需、退货率高或消费者不愿支付溢价。"),
      neg("供应链砍单", "新品节奏调整或良率问题造成库存和订单下修。"),
    ],
    policy: {
      score: 1, label: "支持", confidence: "中",
      summary: "端侧硬件受益于人工智能和先进制造政策，但短期兑现主要由产品周期和真实销量决定。",
      evidence: [fypEvidence("推动人工智能技术创新和新技术新产品大规模应用示范")],
    },
  },
  "energy-storage": {
    asOf: "2026-08-01",
    radarKeys: ["energy"],
    newsKeywords: ["energy storage", "battery storage", "储能", "电化学储能"],
    nodes: [
      node("电芯", "循环寿命、安全和单位成本决定储能全生命周期经济性。", [
        co("300750", "宁德时代"), co("002074", "国轩高科"),
      ]),
      node("PCS 与逆变", "功率转换、并网控制和构网能力决定储能对电网的实际价值。", [
        co("300274", "阳光电源"), co("688390", "固德威"),
      ]),
      node("温控与消防", "热管理和安全预警直接影响事故率、保险和项目准入。", [
        co("002837", "英维克"), co("002126", "银轮股份"),
      ]),
      node("系统集成", "集成商承担电芯选择、控制、交付与长期运维责任。", [
        co("002335", "科华数据"), co("300693", "盛弘股份"),
      ]),
    ],
    watchpoints: [
      pos("装机、调用与市场机制", "截至 2026 年 6 月底已投运新型储能达 1.53 亿千瓦/3.96 亿千瓦时；继续跟踪利用小时、容量补偿、辅助服务和现货价差能否改善收益率。"),
      pos("海外大储订单", "项目中标、并网和回款形成连续兑现。"),
      neg("安全事故", "火灾或召回导致标准提高、项目延期和保险成本上升。"),
      neg("低价竞争与回款", "系统价格过快下降或地方项目回款周期拉长。"),
    ],
    policy: {
      score: 2, label: "强支持", confidence: "高",
      summary: "国家能源局最新报告显示行业已进入规模化并向市场化发展过渡，2026 年上半年新型储能投资同比增长 74.3%；装机高增不等于盈利，核心仍是调用率、市场机制、安全和回款。",
      evidence: [fypEvidence("推动新型储能关键技术创新并完善新型电力系统")],
    },
  },
  "data-element": {
    asOf: "2026-08-01",
    radarKeys: ["tech", "security", "macro"],
    newsKeywords: ["data infrastructure", "data exchange", "数据要素", "公共数据", "数据基础设施"],
    nodes: [
      node("数据基础设施", "算力、存储、数据空间和可信流通设施承载数据的供给与调用。", [
        co("603881", "数据港"), co("603019", "中科曙光"),
      ]),
      node("治理与开发平台", "目录、质量、主数据和分析平台把原始数据转化为可用资产。", [
        co("300166", "东方国信"), co("688031", "星环科技-U"),
      ]),
      node("安全与合规", "权限、隐私计算、审计和防泄漏是数据跨主体流通的前提。", [
        co("002439", "启明星辰"), co("300454", "深信服"),
      ]),
      node("公共数据运营", "授权运营、定价和收益分配机制决定数据产品能否形成收入。", [
        co("600602", "云赛智联"), co("300229", "拓尔思"),
      ]),
    ],
    watchpoints: [
      pos("公共数据授权运营", "地方平台形成标准化数据产品、定价和持续付费客户。"),
      pos("可信数据空间落地", "行业数据空间从试点转向规模采购和跨主体使用。"),
      neg("商业模式难兑现", "确权、定价和收益分配不清，项目停留在建设阶段。"),
      neg("安全与跨境规则收紧", "数据使用范围受限或合规成本显著上升。"),
    ],
    policy: {
      score: 2, label: "强支持，制度建设期", confidence: "高",
      summary: "规划明确激活数据要素潜能并加强高质量数据供给，当前关键在制度、标准和真实交易。",
      evidence: [fypEvidence("激活数据要素潜能，加强高质量数据资源供给和数据治理")],
    },
  },
  gaming: {
    asOf: "2026-08-01",
    radarKeys: ["consumer", "tech"],
    newsKeywords: ["game", "gaming", "游戏", "版号", "Steam"],
    nodes: [
      node("研发与自研 IP", "产品品质、研发周期和长线运营能力决定单款游戏的生命周期。", [
        co("002624", "完美世界"), co("603444", "吉比特"),
      ]),
      node("发行与买量", "渠道、投放回收期和全球发行能力决定新品商业化效率。", [
        co("002555", "三七互娱"), co("002517", "恺英网络"),
      ]),
      node("全球化与多品类", "区域分散、产品矩阵和本地化运营降低单一市场风险。", [
        co("002602", "世纪华通"), co("300002", "神州泰岳"),
      ]),
      node("小游戏与内容生态", "平台流量、分成规则和轻量内容供给决定用户获取效率。", [
        co("300315", "掌趣科技"), co("002517", "恺英网络"),
      ]),
    ],
    watchpoints: [
      pos("重点游戏定档与上线", "版号、预约、测试反馈、流水排名和留存逐步验证。", "未来 3—12 个月"),
      pos("进口/国产版号常态化", "月度审批数量、重点产品覆盖和新品供给保持稳定。", "每月"),
      neg("重点产品延期或不及预期", "测试数据、上线流水或留存明显低于公司目标。"),
      neg("内容与未成年人监管变化", "合规要求、渠道规则或消费限制影响产品运营。"),
    ],
    policy: {
      score: 0, label: "中性：常态审批与强监管并存", confidence: "高",
      summary: "版号审批提供可核验的供给节奏，但内容、未成年人保护和运营合规仍是硬约束。",
      evidence: [
        {
          title: "国产与进口网络游戏审批信息",
          source: "国家新闻出版署",
          date: "持续更新",
          url: GAME_APPROVAL_URL,
          direction: "positive",
        },
        {
          title: "进一步严格管理切实防止未成年人沉迷网络游戏",
          source: "国家新闻出版署",
          date: "2021-08",
          url: GAME_RULE_URL,
          direction: "mixed",
        },
      ],
    },
  },
};

const AI_PLUS_URL = "https://www.gov.cn/zhengce/zhengceku/202508/content_7037862.htm";
const EAST_WEST_URL = "https://www.gov.cn/zhengce/zhengceku/202401/content_6924596.htm";
const LOW_ALTITUDE_URL = "https://www.gov.cn/zhengce/zhengceku/202403/content_6942115.htm";
const LOW_ALTITUDE_STANDARD_URL = "https://www.gov.cn/zhengce/zhengceku/202602/content_7056835.htm";
const SMART_DRIVING_URL = "https://www.gov.cn/zhengce/zhengceku/202401/content_6926711.htm";
const DRUG_SUPPORT_URL = "https://www.moj.gov.cn/pub/sfbgw/gwxw/xwyw/202407/t20240705_501939.html";
const MEDICAL_REFORM_URL = "https://www.gov.cn/zhengce/zhengceku/202406/content_6955905.htm";
const STORAGE_ACTION_URL = "https://www.gov.cn/zhengce/zhengceku/202509/P020250912411822546143.pdf";
const DATA_SPACE_URL = "https://www.gov.cn/zhengce/zhengceku/202411/content_6996363.htm";
const DATA_STANDARD_URL = "https://www.gov.cn/zhengce/zhengceku/202410/content_6978809.htm";
const RARE_EARTH_URL = "https://www.gov.cn/gongbao/2024/issue_11466/202407/content_6963172.html";

interface SectorNodePlan {
  extras: SectorNode[];
  order: string[];
}

const nodePlans: Record<string, SectorNodePlan> = {
  humanoid: {
    extras: [
      node("关键材料与基础件", "轴承、磁材、轻量化材料和编码器位于零部件制造前端，决定成本底座与供应稳定性。", [co("600366", "宁波韵升"), co("603667", "五洲新春")]),
      node("控制器与具身模型", "运动控制器、实时系统和具身模型把感知、规划与执行闭环起来。", [co("300124", "汇川技术"), co("688111", "金山办公")]),
      node("场景部署与运维", "汽车、3C、物流等客户的集成、培训和运维决定机器人能否从样机变成生产资料。", [co("601689", "拓普集团"), co("002008", "大族激光")]),
    ],
    order: ["关键材料与基础件", "精密传动", "电机与驱动", "灵巧手与传感", "控制器与具身模型", "本体与系统集成", "场景部署与运维"],
  },
  "ai-computing": {
    extras: [
      node("半导体设备与材料", "先进制程、封装和高速互连所需设备材料决定算力硬件的供给弹性。", [co("688012", "中微公司"), co("688019", "安集科技")]),
      node("先进存储", "HBM、DDR 与高速存储为模型训练和推理提供带宽与容量。", [co("000021", "深科技"), co("688525", "佰维存储")]),
      node("服务器与交换机", "整机、交换机和集群集成把芯片转化为可交付算力。", [co("000977", "浪潮信息"), co("000938", "紫光股份")]),
      node("数据中心与云服务", "机房建设、云调度和客户上架率决定算力的最终利用率与回报。", [co("603019", "中科曙光"), co("603881", "数据港")]),
    ],
    order: ["半导体设备与材料", "AI 芯片", "先进存储", "封装与 PCB", "服务器与交换机", "高速光互连", "供电与液冷", "数据中心与云服务"],
  },
  hbm: {
    extras: [
      node("TSV 与混合键合", "硅通孔、晶圆减薄和键合工艺决定堆叠密度、热管理与良率。", [co("688072", "拓荆科技"), co("688082", "盛美上海")]),
      node("测试与加速卡集成", "晶圆测试、老化测试和加速卡验证是 HBM 进入算力系统前的最后门槛。", [co("603160", "汇顶科技"), co("300604", "长川科技")]),
    ],
    order: ["关键材料", "制造设备", "存储与接口芯片", "TSV 与混合键合", "先进封装", "测试与加速卡集成"],
  },
  cpo: {
    extras: [
      node("无源光学与连接", "光纤、连接器、透镜和 AWG 等无源器件影响链路损耗、装配精度和可靠性。", [co("300308", "中际旭创"), co("300394", "天孚通信")]),
      node("数据中心系统集成", "交换机、机柜和网络运维决定 CPO 能否在真实集群中大规模部署。", [co("000938", "紫光股份"), co("603019", "中科曙光")]),
    ],
    order: ["光芯片与器件", "无源光学与连接", "光引擎与耦合", "高速光模块", "交换芯片", "数据中心系统集成"],
  },
  semiconductor: {
    extras: [
      node("IP 与芯片设计", "处理器、模拟和专用芯片设计决定下游产品差异化与晶圆需求。", [co("688521", "芯原股份"), co("603986", "兆易创新")]),
      node("封装测试", "先进封装与测试连接设计、制造和终端，是高算力芯片的重要增量。", [co("002156", "通富微电"), co("600584", "长电科技")]),
      node("终端与行业应用", "汽车、工业、通信和消费电子需求决定库存周期与扩产节奏。", [co("002475", "立讯精密"), co("300124", "汇川技术")]),
    ],
    order: ["设备", "材料", "EDA 与设计工具", "IP 与芯片设计", "晶圆制造与封测", "封装测试", "终端与行业应用"],
  },
  "solid-state-battery": {
    extras: [
      node("锂资源与金属负极", "锂盐、锂金属和硅碳材料影响固态电池的成本、界面稳定性和能量密度。", [co("002460", "赣锋锂业"), co("300035", "中科电气")]),
      node("电池包与 BMS", "热管理、状态估计和包体结构把电芯安全性转化为系统可用性。", [co("002594", "比亚迪"), co("300750", "宁德时代")]),
      node("车辆与储能应用", "整车、高端装备与储能客户的验证和采购决定技术路线的商业落地。", [co("601633", "长城汽车"), co("600104", "上汽集团")]),
    ],
    order: ["锂资源与金属负极", "正极与复合材料", "电解质与添加剂", "制造设备", "电芯与整车验证", "电池包与 BMS", "车辆与储能应用"],
  },
  "low-altitude": {
    extras: [
      node("复材与航空原材料", "碳纤维、铝钛合金和航空级元器件构成适航安全与轻量化底座。", [co("300699", "光威复材"), co("688122", "西部超导")]),
      node("起降场与地面保障", "起降点、充换电、维修和气象服务决定航线能否持续运营。", [co("002542", "中化岩土"), co("002097", "山河智能")]),
      node("场景服务与保险", "物流、文旅、应急和城市交通的付费能力决定商业闭环。", [co("002352", "顺丰控股"), co("601021", "春秋航空")]),
    ],
    order: ["复材与航空原材料", "航空核心部件", "定位与链路", "整机与运营", "空管与通信导航", "起降场与地面保障", "场景服务与保险"],
  },
  "smart-driving": {
    extras: [
      node("车规芯片与传感器", "算力芯片、摄像头、雷达和惯导构成感知与计算硬件底座。", [co("603501", "豪威集团"), co("002920", "德赛西威")]),
      node("车路云基础设施", "路侧感知、通信、云控平台和高精地图扩展单车智能边界。", [co("002405", "四维图新"), co("300212", "易华录")]),
      node("出行运营与售后", "Robotaxi、物流和保险维修数据决定高阶驾驶的规模经济与责任成本。", [co("601066", "中信建投"), co("600104", "上汽集团")]),
    ],
    order: ["车规芯片与传感器", "域控制器与计算平台", "感知与算法", "线控底盘", "整车与数据闭环", "车路云基础设施", "出行运营与售后"],
  },
  "innovative-drug": {
    extras: [
      node("靶点与科研工具", "组学、试剂和科研设备支撑早期靶点发现与验证。", [co("688105", "诺唯赞"), co("688133", "泰坦科技")]),
      node("临床研究与注册", "患者入组、临床运营、统计和监管沟通决定管线推进效率。", [co("300759", "康龙化成"), co("603259", "药明康德")]),
      node("生产与质量体系", "原液、制剂和质量体系决定获批后的供应稳定性与成本。", [co("300363", "博腾股份"), co("688202", "美迪西")]),
      node("支付与院内准入", "医保谈判、商保和医院准入影响创新药可及性与放量速度。", [co("600056", "中国医药"), co("601607", "上海医药")]),
    ],
    order: ["靶点与科研工具", "自主研发平台", "ADC 与新技术平台", "CRO/CDMO", "临床研究与注册", "生产与质量体系", "商业化与出海", "支付与院内准入"],
  },
  "power-grid": {
    extras: [
      node("电工材料与核心部件", "硅钢、铜铝、绝缘件和电力电子器件决定设备成本与可靠性。", [co("600019", "宝钢股份"), co("600522", "中天科技")]),
      node("配网与用户侧", "配电自动化、微电网和智能电表把主网能力延伸到终端用户。", [co("601877", "正泰电器"), co("300360", "炬华科技")]),
      node("运维检测与电力服务", "在线监测、巡检和能源管理决定设备全生命周期效率。", [co("300286", "安科瑞"), co("688676", "金盘科技")]),
    ],
    order: ["电工材料与核心部件", "一次设备", "线缆与海缆", "二次设备与调度", "新能源并网", "配网与用户侧", "运维检测与电力服务"],
  },
  defense: {
    extras: [
      node("军工材料与基础件", "钛合金、复材、元器件和高可靠连接构成装备制造底座。", [co("688122", "西部超导"), co("300699", "光威复材")]),
      node("制导通信与信息化", "雷达、数据链、导航和电子对抗决定体系化作战效能。", [co("600879", "航天电子"), co("002025", "航天电器")]),
      node("维修保障与训练", "备件、维修、大修和模拟训练决定装备可用率与全寿命价值。", [co("600316", "洪都航空"), co("002179", "中航光电")]),
    ],
    order: ["军工材料与基础件", "航空发动机", "电子与连接", "制导通信与信息化", "航空整机", "舰船装备", "维修保障与训练"],
  },
  fusion: {
    extras: [
      node("超导与耐辐照原料", "铌锡、稀有金属和耐辐照合金是磁体与堆内构件的上游基础。", [co("688122", "西部超导"), co("600456", "宝钛股份")]),
      node("燃料循环与氚工程", "氘氚供给、包层增殖、净化和安全控制决定聚变堆闭合运行。", [co("000881", "中广核技"), co("601611", "中国核建")]),
      node("工程总装与电站运维", "系统集成、核级建造、调试和运维决定装置从实验走向工程示范。", [co("601611", "中国核建"), co("601985", "中国核电")]),
    ],
    order: ["超导与耐辐照原料", "超导磁体", "第一壁与特种材料", "真空室与主机结构", "加热与电源系统", "燃料循环与氚工程", "工程总装与电站运维"],
  },
  "business-space": {
    extras: [
      node("航天材料与芯片", "高可靠芯片、复材和特种合金决定卫星与火箭的重量、寿命和供应安全。", [co("688122", "西部超导"), co("600879", "航天电子")]),
      node("地面站与终端", "天线、射频、地面测控和用户终端把空间能力连接到真实客户。", [co("002463", "沪电股份"), co("002281", "光迅科技")]),
      node("数据服务与行业应用", "通信、遥感、导航数据在农业、交通和应急中的付费决定最终回报。", [co("300053", "航宇微"), co("002405", "四维图新")]),
    ],
    order: ["航天材料与芯片", "元器件与连接", "卫星制造", "运载火箭", "地面站与终端", "运营与应用", "数据服务与行业应用"],
  },
  "ai-pharma": {
    extras: [
      node("算力、模型与生物数据", "计算平台、基础模型和高质量多组学数据决定候选发现效率。", [co("603019", "中科曙光"), co("300676", "华大基因")]),
      node("临床验证与注册", "临床试验和监管证据决定 AI 候选能否转化为可销售药物。", [co("300759", "康龙化成"), co("603259", "药明康德")]),
      node("药企授权与商业化", "里程碑付款、共同开发和销售能力决定 AI 平台的收入兑现。", [co("600276", "恒瑞医药"), co("688235", "百济神州")]),
    ],
    order: ["算力、模型与生物数据", "药物研发与转化", "临床前模型", "研发服务平台", "临床验证与注册", "医疗设备与影像", "药企授权与商业化"],
  },
  resources: {
    extras: [
      node("采矿与选冶", "资源禀赋、开采配额、回收率和环保成本决定上游供给弹性。", [co("600111", "北方稀土"), co("000960", "锡业股份")]),
      node("高纯材料与功能材料", "提纯、合金、磁材和靶材把资源品转化为高端制造可用材料。", [co("300666", "江丰电子"), co("300748", "金力永磁")]),
    ],
    order: ["采矿与选冶", "稀土", "锗与镓", "铟与锡", "钨与战略小金属", "高纯材料与功能材料"],
  },
  "ai-application": {
    extras: [
      node("算力、模型与数据", "基础模型、推理服务和行业数据决定应用能力、成本与可控性。", [co("603019", "中科曙光"), co("688111", "金山办公")]),
      node("行业实施与系统集成", "咨询、数据治理、流程改造和私有化部署决定企业能否真正使用 AI。", [co("600588", "用友网络"), co("600570", "恒生电子")]),
      node("客户运营与续费", "活跃度、席位扩张、续费和增购验证 AI 是否形成持续收入。", [co("002410", "广联达"), co("300454", "深信服")]),
    ],
    order: ["算力、模型与数据", "模型与语音入口", "办公与企业软件", "内容与创意工具", "工业智能", "行业实施与系统集成", "客户运营与续费"],
  },
  "ai-hardware": {
    extras: [
      node("传感器、电池与材料", "摄像头、MEMS、电池和轻量材料决定终端续航与环境感知。", [co("603501", "豪威集团"), co("300207", "欣旺达")]),
      node("品牌、渠道与服务", "品牌定义产品，渠道教育用户，售后与内容生态决定复购和留存。", [co("002241", "歌尔股份"), co("000100", "TCL科技")]),
    ],
    order: ["端侧芯片", "传感器、电池与材料", "光学与显示", "声学与可穿戴", "精密制造", "品牌、渠道与服务"],
  },
  "energy-storage": {
    extras: [
      node("锂盐与储能材料", "锂盐、正负极、电解液和隔膜决定电芯成本与安全底座。", [co("002460", "赣锋锂业"), co("002709", "天赐材料")]),
      node("EMS 与电网接入", "能量管理、调度交易和构网控制决定储能能否获得多元收益。", [co("300274", "阳光电源"), co("600406", "国电南瑞")]),
      node("项目开发与电力交易", "选址、融资、容量租赁和现货交易决定项目内部收益率。", [co("600905", "三峡能源"), co("600900", "长江电力")]),
    ],
    order: ["锂盐与储能材料", "电芯", "PCS 与逆变", "温控与消防", "系统集成", "EMS 与电网接入", "项目开发与电力交易"],
  },
  "data-element": {
    extras: [
      node("数据采集与供给", "政务、产业和设备数据的标准化采集决定可开发的数据资源规模。", [co("300166", "东方国信"), co("300229", "拓尔思")]),
      node("交易与可信流通", "登记、授权、隐私计算和可信数据空间连接供需双方。", [co("600602", "云赛智联"), co("300454", "深信服")]),
      node("行业应用与收益分配", "金融、制造、医疗等场景的真实付费和收益分配决定商业闭环。", [co("600570", "恒生电子"), co("600588", "用友网络")]),
    ],
    order: ["数据采集与供给", "数据基础设施", "治理与开发平台", "安全与合规", "交易与可信流通", "公共数据运营", "行业应用与收益分配"],
  },
  gaming: {
    extras: [
      node("IP 与内容创意", "文学、美术、音乐和世界观是产品立项与长线衍生价值的源头。", [co("300364", "中文在线"), co("300418", "昆仑万维")]),
      node("引擎与研发工具", "引擎、云服务、测试和生成式工具影响研发效率、画质与跨平台适配。", [co("300624", "万兴科技"), co("300229", "拓尔思")]),
      node("渠道、直播与衍生", "应用商店、直播社区、电竞和授权衍生承接用户触达与长尾收入。", [co("002517", "恺英网络"), co("300315", "掌趣科技")]),
    ],
    order: ["IP 与内容创意", "引擎与研发工具", "研发与自研 IP", "发行与买量", "全球化与多品类", "小游戏与内容生态", "渠道、直播与衍生"],
  },
};

const policyAdditions: Record<string, SectorPolicyEvidence[]> = {
  humanoid: [
    { title: "关于深入实施“人工智能+”行动的意见", source: "国务院", date: "2025-08", url: AI_PLUS_URL, direction: "positive" },
    { title: "2026年度人形机器人与具身智能实景实训专项行动", source: "工业和信息化部、国务院国资委", date: "2026-06", url: HUMANOID_TRAINING_URL, direction: "mixed" },
  ],
  "ai-computing": [
    { title: "关于深入实施“人工智能+”行动的意见", source: "国务院", date: "2025-08", url: AI_PLUS_URL, direction: "positive" },
    { title: "深入实施“东数西算”工程 加快构建全国一体化算力网", source: "国家数据局等", date: "2023-12", url: EAST_WEST_URL, direction: "mixed" },
  ],
  hbm: [{ title: "关于深入实施“人工智能+”行动的意见", source: "国务院", date: "2025-08", url: AI_PLUS_URL, direction: "positive" }],
  cpo: [{ title: "深入实施“东数西算”工程 加快构建全国一体化算力网", source: "国家数据局等", date: "2023-12", url: EAST_WEST_URL, direction: "positive" }],
  semiconductor: [{ title: "关于深入实施“人工智能+”行动的意见", source: "国务院", date: "2025-08", url: AI_PLUS_URL, direction: "positive" }],
  "solid-state-battery": [
    { title: "新型储能规模化建设专项行动方案（2025—2027年）", source: "国家发展改革委等", date: "2025-09", url: STORAGE_ACTION_URL, direction: "mixed" },
    { title: "中国新型储能发展报告（2026）：锂电主导、多技术路线并举", source: "国家能源局", date: "2026-07", url: STORAGE_REPORT_URL, direction: "mixed" },
  ],
  "low-altitude": [
    { title: "通用航空装备创新应用实施方案（2024—2030年）", source: "工业和信息化部等", date: "2024-03", url: LOW_ALTITUDE_URL, direction: "positive" },
    { title: "低空经济标准体系建设指南（2025年版）", source: "市场监管总局等", date: "2026-02", url: LOW_ALTITUDE_STANDARD_URL, direction: "mixed" },
    { title: "加强信息通信业能力建设 支撑低空基础设施发展的实施意见", source: "工业和信息化部等五部门", date: "2026-02", url: LOW_ALTITUDE_INFRA_URL, direction: "mixed" },
  ],
  "smart-driving": [{ title: "智能网联汽车“车路云一体化”应用试点", source: "工业和信息化部等", date: "2024-01", url: SMART_DRIVING_URL, direction: "mixed" }],
  "innovative-drug": [
    { title: "审议通过《全链条支持创新药发展实施方案》", source: "国务院常务会议", date: "2024-07", url: DRUG_SUPPORT_URL, direction: "positive" },
    { title: "深化医药卫生体制改革2024年重点工作任务", source: "国务院办公厅", date: "2024-06", url: MEDICAL_REFORM_URL, direction: "mixed" },
  ],
  "power-grid": [
    { title: "新型储能规模化建设专项行动方案（2025—2027年）", source: "国家发展改革委等", date: "2025-09", url: STORAGE_ACTION_URL, direction: "mixed" },
    { title: "2026年上半年能源形势：电网投资同比增长13.5%", source: "国家能源局", date: "2026-07", url: ENERGY_PRESS_URL, direction: "positive" },
  ],
  defense: [{ title: "“十五五”规划纲要：一体化推进国家战略体系和能力建设", source: "国家发展改革委", date: "2026-03", url: FYP_URL, direction: "mixed" }],
  fusion: [{ title: "“十五五”规划纲要：加强未来能源和可控核聚变前瞻布局", source: "国家发展改革委", date: "2026-03", url: FYP_URL, direction: "positive" }],
  "business-space": [{ title: "“十五五”规划纲要：推动商业航天等新兴产业发展", source: "国家发展改革委", date: "2026-03", url: FYP_URL, direction: "positive" }],
  "ai-pharma": [
    { title: "关于深入实施“人工智能+”行动的意见", source: "国务院", date: "2025-08", url: AI_PLUS_URL, direction: "positive" },
    { title: "审议通过《全链条支持创新药发展实施方案》", source: "国务院常务会议", date: "2024-07", url: DRUG_SUPPORT_URL, direction: "mixed" },
  ],
  resources: [{ title: "稀土管理条例：总量调控、追溯和监督管理", source: "国务院", date: "2024-06", url: RARE_EARTH_URL, direction: "mixed" }],
  "ai-application": [{ title: "关于深入实施“人工智能+”行动的意见", source: "国务院", date: "2025-08", url: AI_PLUS_URL, direction: "positive" }],
  "ai-hardware": [{ title: "关于深入实施“人工智能+”行动的意见", source: "国务院", date: "2025-08", url: AI_PLUS_URL, direction: "positive" }],
  "energy-storage": [
    { title: "新型储能规模化建设专项行动方案（2025—2027年）", source: "国家发展改革委等", date: "2025-09", url: STORAGE_ACTION_URL, direction: "mixed" },
    { title: "中国新型储能发展报告（2026）", source: "国家能源局", date: "2026-07", url: STORAGE_REPORT_URL, direction: "mixed" },
    { title: "2026年上半年能源形势：新型储能投资同比增长74.3%", source: "国家能源局", date: "2026-07", url: ENERGY_PRESS_URL, direction: "positive" },
  ],
  "data-element": [
    { title: "可信数据空间发展行动计划（2024—2028年）", source: "国家数据局", date: "2024-11", url: DATA_SPACE_URL, direction: "positive" },
    { title: "国家数据标准体系建设指南", source: "国家发展改革委等", date: "2024-10", url: DATA_STANDARD_URL, direction: "mixed" },
  ],
  gaming: [],
};

const stagedNodes = (researchKey: string, currentNodes: SectorNode[]) => {
  const plan = nodePlans[researchKey];
  if (!plan) return currentNodes;
  const combined = [...currentNodes, ...plan.extras];
  const orderIndex = new Map(plan.order.map((name, index) => [name, index]));
  const ordered = combined.sort((a, b) => (orderIndex.get(a.name) ?? 999) - (orderIndex.get(b.name) ?? 999));
  const firstCut = Math.ceil(ordered.length / 3);
  const secondCut = Math.ceil((ordered.length * 2) / 3);
  return ordered.map((item, index) => ({
    ...item,
    stage: index < firstCut ? "上游" as const : index < secondCut ? "中游" as const : "下游" as const,
  }));
};

export const sectorResearch: Record<string, SectorResearch> = Object.fromEntries(
  Object.entries(baseSectorResearch).map(([researchKey, research]) => {
    const additions = policyAdditions[researchKey] || [];
    const evidence = [...research.policy.evidence, ...additions].filter(
      (item, index, all) => all.findIndex((candidate) => candidate.title === item.title && candidate.url === item.url) === index,
    );
    return [researchKey, {
      ...research,
      nodes: stagedNodes(researchKey, research.nodes),
      policy: { ...research.policy, evidence },
    }];
  }),
);

const event = (
  date: string,
  status: SectorEvent["status"],
  title: string,
  basis: string,
  judgment: string,
  direction: SectorEvent["direction"],
  confidence: SectorEvent["confidence"],
  source: string,
  url: string,
): SectorEvent => ({ date, status, title, basis, judgment, direction, confidence, source, url });

const announced = (
  date: string,
  title: string,
  basis: string,
  transmission: string,
  confirmation: string,
  invalidation: string,
  direction: SectorEvent["direction"],
  confidence: SectorEvent["confidence"],
  source: string,
  url: string,
) => event(
  date,
  "已官宣",
  title,
  basis,
  `传导：${transmission}；验证：${confirmation}；失效：${invalidation}。`,
  direction,
  confidence,
  source,
  url,
);

export const sectorEvents: Record<string, SectorEvent[]> = {
  humanoid: [
    announced("2026-09-27—10-01", "IROS 2026", "大会官网已公布匹兹堡会议日期，主题覆盖智能机器人系统。", "论文和整机演示更新具身感知、控制与安全路线，带动核心部件送样预期", "国内厂商客户验证、样机迭代或订单公告", "只有概念演示、无性能提升或采购落地", "mixed", "高", "IEEE/RSJ IROS", "https://2026.ieee-iros.org/"),
    announced("2026-12-06—12-09", "IEEE Humanoids 2026", "大会官网已公布法国南锡会议日期。", "人形机器人专项成果集中披露，强化灵巧操作和运动控制路线预期", "可复现实验指标、量产样机和客户试用", "论文指标无法迁移到真实工况或成本不可接受", "positive", "高", "IEEE-RAS Humanoids", "https://2026.ieee-humanoids.org/"),
    announced("2027-01-06—01-09", "CES 2027", "CES 官网已公布拉斯维加斯举办日期。", "消费和服务机器人新品集中发布，打开场景与渠道预期", "定价、首批出货、渠道覆盖和复购数据", "停留在展示机或上市延期", "mixed", "高", "Consumer Technology Association", "https://www.ces.tech/about-ces/about-ces/"),
    announced("2027-05-10—05-13", "Automate 2027", "Automate 官网已公布芝加哥展会日期。", "工业机器人与自动化采购交流增加，推动人形机器人从样机向工厂试用", "付费试点、产线节拍和单位运维成本", "试点无法满足安全、节拍或投资回报要求", "positive", "高", "Association for Advancing Automation", "https://www.automateshow.com/attend"),
  ],
  "ai-computing": [
    announced("2026-08-23—08-25", "Hot Chips 2026", "大会官网已公布帕洛阿尔托会议日期。", "新一代 AI 芯片与互连架构披露，重估算力密度和供应链需求", "实测性能、功耗、量产时间和客户导入", "纸面峰值不能转化为系统效率或量产延期", "mixed", "高", "Hot Chips", "https://hotchips.org/"),
    announced("2026-10-20—10-22", "NVIDIA GTC Berlin 2026", "NVIDIA 官网已公布柏林大会日期。", "平台路线和欧洲 AI 基建合作释放，带动服务器、网络与液冷需求预期", "整机订单、云实例上线和数据中心资本开支", "合作停留在意向或交付受供给与电力约束", "positive", "高", "NVIDIA", "https://www.nvidia.com/en-eu/gtc/conference-schedule/"),
    announced("2026-11-15—11-20", "SC26", "SC26 官网已公布芝加哥会议日期。", "超算与 AI 集群方案集中验证，影响互连、存储和能效路线选择", "基准测试、客户部署和集群稳定性", "基准不可复现或部署成本显著超预期", "mixed", "高", "SC Conference", "https://sc26.supercomputing.org/program/"),
    announced("2027-03-14—03-18", "NVIDIA GTC 2027", "GTC FAQ 已公布圣何塞大会日期。", "下一代加速平台和生态更新形成年度算力采购锚点", "产品规格、供货节奏、云厂商资本开支与订单", "路线延期、性能功耗不达预期或客户削减资本开支", "positive", "高", "NVIDIA GTC", "https://www.nvidia.com/gtc/faq/"),
  ],
  hbm: [
    announced("2026-08-23—08-25", "Hot Chips 2026", "大会官网已公布日期，议程面向高性能芯片系统。", "AI 芯片内存子系统披露提高 HBM 容量和带宽需求可见度", "芯片规格、HBM 堆叠配置和量产版本", "系统改用较低容量配置或量产推迟", "positive", "高", "Hot Chips", "https://hotchips.org/"),
    announced("2026-09-02—09-04", "SEMICON Taiwan 2026", "展会官网已公布日期并覆盖先进封装供应链。", "HBM、混合键合与封装设备交流加速设备材料验证", "客户认证、设备验收和良率爬坡", "展示参数无法通过客户验证或扩产收缩", "positive", "高", "SEMICON Taiwan", "https://www.semicontaiwan.org/en/about/overview"),
    announced("2027-02-02—02-04", "DesignCon 2027", "大会官网已公布圣克拉拉举办日期。", "高速接口与信号完整性方案更新，推动 HBM 及先进封装测试需求", "接口规范、测试设备订单和工程验证", "性能瓶颈未改善或方案不进入量产平台", "mixed", "高", "DesignCon", "https://www.designcon.com/"),
    announced("2027-03-14—03-18", "NVIDIA GTC 2027", "GTC FAQ 已公布大会日期。", "新平台内存配置决定下一轮 HBM 单机用量与代际切换", "正式规格、供应商份额和平台出货计划", "规格降配、供给替代或平台延期", "positive", "高", "NVIDIA GTC", "https://www.nvidia.com/gtc/faq/"),
  ],
  cpo: [
    announced("2026-09-20—09-24", "ECOC 2026", "大会官网已公布会议和展览日期。", "1.6T、CPO 与硅光产品集中发布，推动送样和速率升级预期", "客户测试、模块良率和批量订单", "样品无法通过可靠性测试或成本不具优势", "positive", "高", "ECOC", "https://www.ecoc2026.org/"),
    announced("2026-11-15—11-20", "SC26", "大会官网已公布芝加哥会议日期。", "AI 集群互连方案现场验证，检验 CPO 的功耗与密度价值", "集群部署、端口规模和运行稳定性", "可插拔光模块继续满足需求或 CPO 维护成本过高", "mixed", "高", "SC Conference", "https://sc26.supercomputing.org/program/"),
    announced("2027-03-07—03-11", "OFC 2027", "OFC 官网已公布洛杉矶会议日期。", "光通信年度新品和客户测试结果集中披露，决定 CPO 商用节奏", "量产发布、客户名单、可靠性和订单", "反复送样但无量产客户或良率低于门槛", "positive", "高", "Optica OFC", "https://www.ofcconference.org/home/"),
    announced("2027-03-14—03-18", "NVIDIA GTC 2027", "GTC FAQ 已公布大会日期。", "AI 平台网络架构更新可能把 CPO 从技术选项推向系统配置", "交换机规格、合作伙伴与交付时间", "平台继续沿用传统可插拔方案或发布延期", "mixed", "高", "NVIDIA GTC", "https://www.nvidia.com/gtc/faq/"),
  ],
  semiconductor: [
    announced("2026-08-23—08-25", "Hot Chips 2026", "大会官网已公布日期。", "先进芯片架构披露带动制程、封装和设备需求预期", "量产节点、晶圆投片和设备订单", "设计发布后量产推迟或客户需求下修", "mixed", "高", "Hot Chips", "https://hotchips.org/"),
    announced("2026-09-02—09-04", "SEMICON Taiwan 2026", "展会官网已公布日期和产业覆盖。", "先进制程与封装路线更新，影响设备材料验证和扩产预期", "设备验收、客户认证与资本开支", "展会发布无后续采购或产能利用率下降", "positive", "高", "SEMICON Taiwan", "https://www.semicontaiwan.org/en/about/overview"),
    announced("2026-10-13—10-15", "SEMICON West 2026", "展会官网已公布旧金山举办日期。", "全球设备材料厂商更新订单与技术路线，提供周期拐点线索", "龙头订单、交付周期和晶圆厂资本开支", "订单口径未改善或出口限制加剧", "mixed", "高", "SEMICON West", "https://www.semiconwest.org/about/welcome"),
    announced("2026-11-10—11-13", "electronica 2026", "展会官网已公布慕尼黑举办日期。", "汽车和工业电子需求反馈传导至模拟、功率和成熟制程景气", "终端订单、渠道库存和产能利用率", "去库存停滞或终端需求继续走弱", "mixed", "高", "Messe München electronica", "https://electronica.de/en/"),
  ],
  "solid-state-battery": [
    announced("2026-10-09—10-11", "SNEC ES+ 2026", "展会官网已公布上海举办窗口。", "固态与半固态电芯、材料和设备样品集中展示，提升中试预期", "第三方能量密度、循环、安全测试和产线招标", "样品指标不可复现或量产成本过高", "mixed", "中", "SNEC ES+", "https://www.snec.org.cn/"),
    announced("2026-10-12—10-15", "The Battery Show North America 2026", "主办方官网已公布底特律展会日期。", "电池产业链技术和客户交流加速材料认证与设备送样", "车企定点、中试线订单和认证进度", "无车企验证或样品寿命不达标", "positive", "高", "The Battery Show", "https://www.thebatteryshow.com/"),
    announced("2027-01-06—01-09", "CES 2027", "CES 官网已公布拉斯维加斯举办日期。", "车企和消费电子新品可能披露固态电池应用路线", "明确车型、装机量、量产年份和供应商", "只展示概念车或量产节点继续后移", "mixed", "高", "Consumer Technology Association", "https://www.ces.tech/about-ces/about-ces/"),
    announced("2027-06-08—06-10", "ees Europe 2027", "展会官网已公布慕尼黑举办日期。", "欧洲储能和电池项目需求检验固态技术的商业适配性", "项目采购、系统认证和全生命周期成本", "成本与安全优势不足以替代成熟锂电路线", "mixed", "高", "ees Europe", "https://www.ees-europe.com/for-visitors"),
  ],
  "low-altitude": [
    announced("2026-11-10—11-15", "第十六届中国国际航空航天博览会", "中国航展官网已开放本届展会信息。", "eVTOL、无人机、空管和保障方案集中发布，提供适航与订单线索", "适航进展、采购合同、运营航线和利用率", "展示后无取证、交付或可持续运营", "mixed", "中", "中国航展", "https://www.airshow.com.cn/Category_1216/Index.aspx"),
    announced("2027-01-06—01-09", "CES 2027", "CES 官网已公布拉斯维加斯举办日期。", "无人机与智能出行产品进入消费和企业渠道视野", "定价、渠道订单和真实场景运营数据", "概念产品未上市或监管不允许商业运营", "mixed", "高", "Consumer Technology Association", "https://www.ces.tech/about-ces/about-ces/"),
    announced("2027-04-14—04-17", "AERO Friedrichshafen 2027", "AERO 官网已公布展会日期。", "通航与电动航空厂商集中交流，推动适航、动力和运营模式验证", "取证里程碑、飞机订单和交付排期", "认证延期或单位经济性不成立", "positive", "高", "AERO Friedrichshafen", "https://www.aero-expo.com/"),
    announced("2027-06-14—06-20", "Paris Air Show 2027", "巴黎航展官网已公布举办日期。", "全球航空订单与新型飞行器发布形成产业链需求窗口", "正式订单、供应商定点和交付节奏", "意向订单取消或供应链无法按期交付", "mixed", "高", "Paris Air Show", "https://www.siae.fr/en/"),
  ],
  "smart-driving": [
    announced("2026-11-27—12-06", "广州国际汽车展览会 2026", "车展官网已公布公众展期。", "智能驾驶新车型和高阶功能集中发布，推动传感器与域控配置升级", "车型售价、搭载率、交付量和用户接管数据", "高阶功能延期或消费者付费率偏低", "positive", "高", "广州国际汽车展览会", "https://www.autoguangzhou.org.cn/index.html"),
    announced("2027-01-06—01-09", "CES 2027", "CES 官网已公布举办日期。", "全球车企和芯片厂更新智驾平台，影响算力与传感器路线", "量产车型、定点项目和道路测试结果", "方案停留在概念车或法规限制扩大", "mixed", "高", "Consumer Technology Association", "https://www.ces.tech/about-ces/about-ces/"),
    announced("2027-04-25—05-02", "上海国际汽车工业展览会 2027", "上海市政府公开信息已公布展期。", "国内车企新车周期集中，验证高阶智驾渗透率和价格带", "正式配置表、订单、交付与功能开通率", "降配、延期或智驾事故导致监管收紧", "positive", "高", "上海市人民政府", "https://english.shanghai.gov.cn/en-Editorspick-DoBusiness/20260430/7445580f34f8413da68ba0ed2d838385.html"),
    announced("2027-09-07—09-12", "IAA MOBILITY 2027", "IAA 官网已公布慕尼黑活动日期。", "欧洲车企发布智能电动平台，带动海外供应链定点预期", "海外车型定点、法规批准和量产订单", "欧洲需求疲弱或供应链本地化排除相关厂商", "mixed", "高", "IAA MOBILITY", "https://www.iaa-mobility.com/en/exhibitors"),
  ],
  "innovative-drug": [
    announced("2026-10-23—10-27", "ESMO Congress 2026", "ESMO 官网已公布马德里大会日期。", "肿瘤管线数据读出改变授权、获批和商业化预期", "主要终点、样本量、安全性和监管沟通", "疗效无统计或临床意义、毒性超预期", "mixed", "高", "ESMO", "https://www.esmo.org/meeting-calendar/esmo-congress-2026"),
    announced("2027-01-30—02-03", "SLAS 2027", "SLAS 官网已公布新奥尔良会议日期。", "自动化筛选和实验平台更新提高早研效率预期", "可复现筛选命中率、合作项目和候选物推进", "平台效率无法转化为候选物或合作收入", "mixed", "高", "SLAS", "https://www.slas.org/events-calendar/slas2027-international-conference-exhibition/schedule-at-a-glance/"),
    announced("2027-04-02—04-07", "AACR Annual Meeting 2027", "AACR 官网已公布奥兰多大会日期。", "早期肿瘤临床和转化数据集中披露，决定管线估值分化", "摘要全文、剂量反应、缓解持续时间和安全性", "数据不及同类或后续开发停止", "mixed", "高", "AACR", "https://www.aacr.org/about-the-aacr/newsroom/annual-meeting/"),
    announced("2027-06-04—06-08", "ASCO Annual Meeting 2027", "ASCO 官网已公布芝加哥大会日期。", "关键临床结果和治疗指南讨论影响商业峰值预期", "关键终点、亚组一致性、注册进度与医生反馈", "关键终点失败或竞争格局显著恶化", "mixed", "高", "ASCO", "https://www.asco.org/annual-meeting/"),
  ],
  "power-grid": [
    announced("2026-08-23—08-28", "CIGRE Paris Session 2026", "CIGRE 官网已公布巴黎会议日期。", "大电网、直流输电和数字化技术路线集中交流，推动设备升级预期", "技术规范、示范项目、招标和设备交付", "方案未进入标准或项目投资推迟", "positive", "高", "CIGRE", "https://session.cigre.org/"),
    announced("2026-11-10—11-12", "Enlit Europe 2026", "Enlit 官网已公布维也纳活动日期。", "欧洲电网投资和数字能源项目释放海外需求线索", "中标、认证、在手订单和回款", "项目延迟或贸易壁垒抬升", "mixed", "高", "Enlit Europe", "https://www.enlit.world/events/enlit-europe"),
    announced("2026-11-16—11-19", "RE+ 2026", "RE+ 官网已公布拉斯维加斯展会日期。", "构网型储能与电网接入方案发布，推动逆变器和保护设备需求", "并网认证、项目订单与调度运行数据", "认证失败或项目经济性恶化", "positive", "高", "RE+", "https://www.re-plus.com/"),
    announced("2027-04-19—04-22", "IEEE PES Grid Edge 2027", "IEEE PES 活动页已公布圣迭戈会议窗口。", "配电网边缘控制和韧性方案验证，带动数字化设备试点", "公用事业采购、实际降损和故障恢复指标", "试点无法形成规模采购或收益不清晰", "mixed", "中", "IEEE Power & Energy Society", "https://ieee-pes.org/events/"),
  ],
  defense: [
    announced("2026-10-12—10-14", "AUSA Annual Meeting 2026", "AUSA 官网已公布华盛顿会议日期。", "陆军装备和无人系统需求交流影响军工电子与平台预期", "预算、正式合同和交付计划", "预算延迟或展示项目无采购", "mixed", "高", "Association of the United States Army", "https://meetings.ausa.org/annual/2026/"),
    announced("2026-11-10—11-15", "第十六届中国国际航空航天博览会", "中国航展官网已开放本届信息。", "新型号、航电和无人系统展示提高产业关注度", "公开定型、采购与交付线索", "不能从展品推导订单，且无后续公开验证", "mixed", "中", "中国航展", "https://www.airshow.com.cn/Category_1216/Index.aspx"),
    announced("2027-01-25—01-29", "IDEX 2027", "IDEX 官网已公布阿布扎比展会日期。", "国际防务需求和无人化方案交流带来出口线索", "出口许可、正式订单和回款", "意向未转合同或地缘政治阻断交付", "mixed", "高", "IDEX", "https://www.idexuae.ae/"),
    announced("2027-06-14—06-20", "Paris Air Show 2027", "巴黎航展官网已公布日期。", "航空平台和供应链订单发布影响军民两用环节景气", "合同、供应商定点和交付节奏", "意向订单取消或产能瓶颈延迟确认收入", "mixed", "高", "Paris Air Show", "https://www.siae.fr/en/"),
  ],
  fusion: [
    announced("2026-09-20—09-25", "SOFT 2026", "大会官网已公布法国普罗旺斯地区艾克斯会议日期。", "聚变材料、磁体和氚工程成果更新工程化路线预期", "装置采购、样机测试和工程参数", "关键材料或氚闭环指标无实质进展", "mixed", "高", "Symposium on Fusion Technology", "https://soft2026.org/"),
    announced("2026-10-11—10-16", "AAPPS-DPP 2026", "AAPPS-DPP 官网已公布会议日期。", "亚太等离子体研究成果交流带动装置与诊断需求预期", "实验结果、装置升级和设备招标", "成果无法复现或无工程项目承接", "mixed", "高", "AAPPS-DPP", "https://www.aappsdpp.org/DPP2026/"),
    announced("2026-11-02—11-06", "APS DPP 2026", "APS 官网已公布芝加哥会议日期。", "惯性与磁约束聚变研究进展更新技术分支胜率", "能量增益、重复频率和装置运行数据", "关键指标停滞或路线所需成本不可承受", "mixed", "高", "American Physical Society", "https://www.aps.org/events/2026/68th-annual-meeting-dpp-gec"),
    announced("2027-06-20—06-24", "IEEE PPC-SOFE 2027", "IEEE NPSS 已公布联合会议窗口。", "聚变工程和脉冲功率设备讨论推动工程部件需求验证", "工程合同、设备交付和可靠性测试", "技术路线未进入装置建设或项目融资中断", "mixed", "中", "IEEE NPSS", "https://ieee-npss.org/technical-committees/pulsed-power-science-and-technology/"),
  ],
  "business-space": [
    announced("2026-08-23—08-26", "SmallSat 2026", "大会官网已公布盐湖城会议日期。", "小卫星、载荷与地面系统需求交流提高供应链订单可见度", "星座融资、采购合同、发射排期和交付", "融资中断或发射计划推迟", "positive", "高", "SmallSat Conference", "https://smallsat.org/"),
    announced("2026-10-05—10-09", "International Astronautical Congress 2026", "IAC 官网已公布安塔利亚会议日期。", "全球航天机构与商业公司发布合作和任务计划", "签约、许可证、任务里程碑和资金到位", "合作仅为备忘录或任务延期", "mixed", "高", "International Astronautical Federation", "https://www.iac2026.org/"),
    announced("2026-11-10—11-15", "第十六届中国国际航空航天博览会", "中国航展官网已开放本届展会信息。", "商业火箭、卫星与终端集中展示，形成国内供应链线索", "发射许可、订单、成功率和星座资本开支", "展品无后续任务或发射失败", "mixed", "中", "中国航展", "https://www.airshow.com.cn/Category_1216/Index.aspx"),
    announced("2027-06-14—06-20", "Paris Air Show 2027", "巴黎航展官网已公布日期。", "全球航空航天订单与合作发布带动商业航天曝光和融资", "正式订单、融资、发射和交付节奏", "意向未落地或资金链恶化", "mixed", "高", "Paris Air Show", "https://www.siae.fr/en/"),
  ],
  "ai-pharma": [
    announced("2026-10-23—10-27", "ESMO Congress 2026", "ESMO 官网已公布大会日期。", "AI 发现候选物的临床数据检验平台转化效率", "临床终点、安全性、研发周期和合作里程碑", "模型演示不能形成候选物或临床数据不佳", "mixed", "高", "ESMO", "https://www.esmo.org/meeting-calendar/esmo-congress-2026"),
    announced("2026-12-06—12-12", "NeurIPS 2026", "NeurIPS 官网已公布悉尼会议日期。", "药物生成和生物基础模型方法更新提高研发工具预期", "开放基准、湿实验复现和药企采用", "仅在封闭数据集有效或无法湿实验验证", "mixed", "高", "NeurIPS", "https://neurips.cc/?event=20031"),
    announced("2027-01-30—02-03", "SLAS 2027", "SLAS 官网已公布新奥尔良会议日期。", "实验室自动化与 AI 筛选结合推动闭环研发平台落地", "机器人实验吞吐、命中率和付费客户", "自动化成本抵消效率收益", "positive", "高", "SLAS", "https://www.slas.org/events-calendar/slas2027-international-conference-exhibition/schedule-at-a-glance/"),
    announced("2027-05-18—05-20", "Bio-IT World 2027", "大会官网已公布波士顿举办日期。", "药企数据和 AI 基础设施采购需求集中释放", "企业合同、部署范围和续费扩容", "试点不进入生产流程或合规成本过高", "positive", "高", "Bio-IT World", "https://www.bio-itworldexpo.com/"),
  ],
  resources: [
    announced("2026-09-10—09-12", "2026 中国国际矿业大会", "大会通知已公布天津举办日期。", "关键矿产供给、投资和绿色开发信息更新资源预期", "项目审批、产量、配额、库存和长期合同", "项目延期或需求端去库存", "mixed", "高", "中国国际矿业大会", "https://www.china-mining.org.cn/Article/Detail?auto=5556&col=15"),
    announced("2027-02-08—02-11", "Mining Indaba 2027", "大会官网已公布开普敦日期。", "非洲矿业融资与项目合作影响铜、锂和关键矿产中期供给", "融资关闭、建设开工和包销协议", "融资失败、许可延误或成本超支", "mixed", "高", "Mining Indaba", "https://miningindaba.com/home"),
    announced("2027-03-07—03-10", "PDAC Convention 2027", "PDAC 官网已公布多伦多大会日期。", "勘探与融资活动提供新增资源项目和风险偏好线索", "融资额、发现量、可研和并购交易", "资本市场降温或资源量无法经济开采", "mixed", "高", "Prospectors & Developers Association of Canada", "https://pdac.ca/convention-2027"),
    announced("2027-04-19—04-20", "Critical Minerals North America 2027", "大会官网已公布纽约会议日期。", "关键矿产政策与产业链本地化讨论影响贸易流和溢价", "采购协议、补贴细则和新增产能", "政策延期或替代供应快速释放", "mixed", "高", "Critical Minerals North America", "https://www.criticalmineralsnorthamerica.com/"),
  ],
  "ai-application": [
    announced("2026-11-17—11-20", "Microsoft Ignite 2026", "Microsoft 官网已公布旧金山与线上活动日期。", "企业 AI 和 Agent 产品更新加速办公及行业应用渗透", "正式定价、付费席位、客户案例和续费", "功能免费捆绑或客户使用率偏低", "positive", "高", "Microsoft Ignite", "https://ignite.microsoft.com/"),
    announced("2026-11-30—12-04", "AWS re:Invent 2026", "AWS 官网已公布拉斯维加斯大会日期。", "云上模型与 Agent 服务发布降低企业部署门槛", "云消费增量、生产环境客户和伙伴订单", "试用无法转生产或推理成本过高", "positive", "高", "Amazon Web Services", "https://aws.amazon.com/events/reinvent/"),
    announced("2027-02-09—02-11", "AI DevWorld 2027", "大会官网已公布圣何塞日期。", "开发框架和 Agent 工具链集中展示，影响应用开发效率", "开发者采用、企业合同和活跃应用", "工具同质化且缺乏付费转化", "mixed", "高", "AI DevWorld", "https://aidevworld.com/"),
    announced("2027-03-14—03-18", "NVIDIA GTC 2027", "GTC FAQ 已公布大会日期。", "模型、推理和行业解决方案更新带动生态应用需求", "客户部署、软件收入和推理调用量", "硬件发布挤压软件价值或应用回报不清晰", "positive", "高", "NVIDIA GTC", "https://www.nvidia.com/gtc/faq/"),
  ],
  "ai-hardware": [
    announced("2026-09-04—09-08", "IFA Berlin 2026", "IFA 官网已公布柏林展会日期。", "AI PC、眼镜与可穿戴新品集中发布，检验端侧 AI 产品形态", "售价、渠道、首批出货和使用频次", "续航、重量或体验不足导致退货", "mixed", "高", "IFA Berlin", "https://www.ifa-berlin.com/"),
    announced("2027-01-06—01-09", "CES 2027", "CES 官网已公布拉斯维加斯日期。", "全球消费电子厂商发布端侧 AI 硬件，推动芯片和零部件定点", "量产型号、供应商份额、渠道订单和激活量", "概念机不量产或用户需求不足", "positive", "高", "Consumer Technology Association", "https://www.ces.tech/about-ces/about-ces/"),
    announced("2027-03-01—03-04", "MWC Barcelona 2027", "MWC 官网已公布巴塞罗那日期。", "手机与连接设备更新端侧模型和通信能力", "旗舰机搭载率、运营商合作和销量", "功能缺乏差异化或成本转嫁失败", "mixed", "高", "GSMA MWC Barcelona", "https://www.mwcbarcelona.com/"),
    announced("2027-06-01—06-04", "COMPUTEX 2027", "COMPUTEX 官方信息已公布台北日期。", "AI PC 与边缘硬件平台集中更新，影响处理器和散热供应链", "OEM 机型、出货指引、BOM 份额和库存", "换机需求不足或渠道库存上升", "positive", "高", "TAITRA COMPUTEX", "https://www.computex.biz/NewsReleaseDetail.aspx?category=68&index=42846"),
  ],
  "energy-storage": [
    announced("2026-10-09—10-11", "SNEC ES+ 2026", "展会官网已公布上海举办窗口。", "储能系统与电芯方案发布影响国内招标参数和价格预期", "中标价、订单、项目收益率和并网数据", "低价竞争继续侵蚀利润或项目延期", "mixed", "中", "SNEC ES+", "https://www.snec.org.cn/"),
    announced("2026-10-12—10-15", "The Battery Show North America 2026", "主办方官网已公布底特律日期。", "电芯、热管理和系统安全技术交流推动海外认证", "UL 认证、客户订单和本地产能利用率", "认证延期或贸易成本抬升", "positive", "高", "The Battery Show", "https://www.thebatteryshow.com/"),
    announced("2026-11-16—11-19", "RE+ 2026", "RE+ 官网已公布拉斯维加斯日期。", "储能系统和构网型 PCS 方案集中发布，释放海外渠道线索", "合同、并网、回款和毛利率", "订单取消或项目融资成本上升", "positive", "高", "RE+", "https://www.re-plus.com/"),
    announced("2027-06-08—06-10", "ees Europe 2027", "展会官网已公布慕尼黑日期。", "欧洲储能采购和政策需求检验系统集成商竞争力", "项目中标、认证、在手订单和售后成本", "欧洲需求放缓或本地化壁垒提高", "mixed", "高", "ees Europe", "https://www.ees-europe.com/for-visitors"),
  ],
  "data-element": [
    announced("2026-08", "2026 中国国际大数据产业博览会", "新华社公开信息明确大会将于 2026 年 8 月在贵阳举办。", "数据要素、可信数据空间和行业应用项目集中展示，推动试点扩围", "跨主体调用量、付费产品、收益分配和采购合同", "只有平台揭牌、无真实交易和持续调用", "mixed", "中", "新华社", "https://english.news.cn/20260430/233cf095f5b349a683ea106739f1071a/c.html"),
    announced("2026-11-30—12-04", "AWS re:Invent 2026", "AWS 官网已公布大会日期。", "云端数据治理和 AI 数据服务发布降低企业数据开发门槛", "生产客户、数据调用、云消费和生态订单", "服务停留在试用或合规限制阻碍共享", "positive", "高", "Amazon Web Services", "https://aws.amazon.com/events/reinvent/"),
    announced("2027-03-08—03-10", "Gartner Data & Analytics Summit 2027", "Gartner 官网已公布奥兰多会议日期。", "企业数据治理与 AI 决策采购议题集中，形成软件需求线索", "预算、合同、席位扩张和续费", "企业削减数据项目或投资回报不清晰", "mixed", "高", "Gartner", "https://www.gartner.com/en/conferences/na/data-analytics-us"),
    announced("2027-05-18—05-20", "Data Innovation Summit 2027", "大会官网已公布斯德哥尔摩日期。", "企业数据平台和数据产品案例集中验证商业化路径", "真实客户、部署范围、付费与数据复用率", "案例不可复制或缺乏持续收入", "positive", "高", "Data Innovation Summit", "https://datainnovationsummit.com/region/nordics/about/"),
  ],
  gaming: [
    announced("2026-07-31—08-03", "ChinaJoy 2026", "ChinaJoy 官方信息已公布上海举办窗口。", "新品试玩、发行合作和玩家反馈形成后续上线预期", "预约、测试留存、发行签约和上线排期", "展台热度不能转化为留存与流水", "positive", "高", "ChinaJoy", "https://www.chinajoy.net/"),
    announced("2026-08-26—08-30", "gamescom 2026", "gamescom 官网已公布科隆展会日期。", "全球新品与发行合作集中，影响出海和主机游戏预期", "海外发行、愿望单、媒体评分和上线销量", "曝光无转化或发行延期", "positive", "高", "gamescom", "https://www.gamescom.global/en/tickets/buy-tickets"),
    announced("2026-09-17—09-21", "Tokyo Game Show 2026", "TGS 官网已公布幕张展会日期。", "亚洲市场新品和渠道反馈影响产品排期与区域发行", "试玩口碑、发行合作、预约和正式档期", "口碑不佳或本地化发行取消", "mixed", "高", "Computer Entertainment Supplier's Association", "https://tgs.cesa.or.jp/2026/en"),
    announced("2026-10-29", "《影之刃零》全球发售", "游戏官方网站已明确 2026 年 10 月 29 日发售。", "国产高规格单机销量检验买断制和全球发行空间", "Steam/主机销量、玩家评价和长尾收入", "制作方非 A 股公司，且热度不能转化为相关上市公司业绩", "mixed", "高", "S-GAME", "https://pbz.s-game.com/"),
    announced("2026-12-10", "The Game Awards 2026", "主办方 FAQ 已公布颁奖礼日期。", "奖项与新作预告集中提高重点产品全球曝光", "愿望单、渠道流量、预售和发行档期", "曝光短暂且无新增用户转化", "mixed", "高", "The Game Awards", "https://thegameawards.com/faq"),
    announced("2027-03-01—03-05", "Game Developers Conference 2027", "GDC 官网已公布旧金山日期。", "开发工具、商业化和发行趋势更新影响研发效率与供给", "工具采用、研发周期、发行签约和上线产品", "技术演示不降低成本或行业项目继续收缩", "mixed", "高", "Game Developers Conference", "https://gdconf.com/"),
  ],
};
