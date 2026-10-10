"""Explicit, reviewable audit vocabulary; matches are observations, not field coverage."""
TAXONOMY={
'生物制药':[
('mRNA therapeutics','mRNA治疗',r'\bmrna\b|messenger rna'),('antibody engineering','抗体工程',r'antibod.{0,70}(engineer|design|affinity|maturation)|engineer.{0,40}antibod'),
('gene therapy','基因治疗',r'gene therap|基因治疗'),('CAR T cell therapy','CAR-T细胞治疗',r'car.?t\b|chimeric antigen receptor'),('protein engineering','蛋白质工程',r'protein.{0,40}(engineer|design)|directed evolution'),
('lipid nanoparticle delivery','脂质纳米颗粒递送',r'lipid nanoparticle|lipid nanocarrier|\blnps?\b'),('vaccine design','疫苗设计',r'vaccin|疫苗'),('synthetic biology','合成生物学',r'synthetic biolog|合成生物'),('biomanufacturing','生物制造',r'biomanufact|bioprocess|bioreactor|fermentation|生物制造'),
('drug discovery','药物发现',r'drug discover|drug design|药物发现'),('high throughput screening','高通量筛选',r'high.throughput.{0,35}screen|高通量筛选'),('pharmaceutical process development','制药工艺',r'pharmaceutical.{0,40}(process|manufact)|bioprocess|purification.{0,50}(protein|antibod)|制药工艺'),
('bispecific antibodies','双特异性抗体',r'bispecific|双特异性'),('antibody drug conjugates','抗体偶联药物',r'antibody.drug conjugat|抗体偶联'),('CRISPR gene editing','基因编辑',r'crispr|gene edit|genome edit|基因编辑'),('AAV vectors','AAV载体',r'adeno.associated|\baav\b'),('stem cell therapy','干细胞治疗',r'stem cell.{0,60}(therap|treat|transplant)|干细胞治疗'),
('bioinformatics drug discovery','生物信息学药物研发',r'bioinformatic|computational.{0,30}drug|drug.{0,40}(docking|predict)'),('organoid drug screening','类器官筛选',r'organoid|类器官'),('continuous bioprocessing','连续生物工艺',r'continuous.{0,35}(bioprocess|biomanufact|culture)|perfusion culture'),('biosimilars','生物类似药',r'biosimilar|生物类似药'),
('pharmacokinetics biologics','生物药药代动力学',r'pharmacokinetic|pharmacodynamic|药代动力'),('RNA delivery','RNA递送',r'rna.{0,35}deliver|deliver.{0,35}rna'),('microbial cell factories','微生物细胞工厂',r'microbial cell factor|metabolic engineer|代谢工程'),
('preclinical models and safety','临床前模型与安全性',r'preclinical|toxicolog|动物模型|临床前'),('protein formulation and stability','蛋白制剂与稳定性',r'protein.{0,50}(formulation|stabil)|antibod.{0,50}(formulation|stabil)|lyophili'),('cell therapy manufacturing','细胞治疗生产与质控',r'cell therap.{0,100}(manufact|quality|scal)|\bgmp\b.{0,50}cell'),('viral vector manufacturing','病毒载体生产',r'viral vector.{0,100}(manufact|purif|produc)|aav.{0,70}(manufact|purif)'),('multiomics and biomarkers','多组学与生物标志物',r'multi.omics|proteomic|pharmacogenom|biomarker')],
'软件与通信':[
('large language models','大语言模型',r'large language model|\bllms?\b|大语言模型'),('distributed systems','分布式系统',r'distributed (system|comput|consensus)|分布式系统'),('6G wireless communications','6G通信',r'\b6g\b'),('machine learning','机器学习',r'machine learning|deep learning|reinforcement learning|机器学习|深度学习'),('software engineering','软件工程',r'software engineer|software test|code generat|software defect|软件工程'),
('operating systems','操作系统',r'operating system|kernel schedul|操作系统'),('database systems','数据库',r'database|query optimiz|数据库'),('cloud computing','云计算',r'cloud comput|serverless|云计算'),('network security','网络安全',r'network secur|cybersecur|intrusion detect|malware|网络安全'),('edge computing','边缘计算',r'edge comput|mobile edge|边缘计算'),('internet of things','物联网',r'internet of things|\biot\b|物联网'),('optical communications','光通信',r'optical commun|optical fiber.{0,30}(transmiss|commun)|光通信'),
('5G networks','5G网络',r'\b5g\b'),('federated learning','联邦学习',r'federated learn|联邦学习'),('network protocols','网络协议',r'network protocol|\btcp\b|\bquic\b|\bbgp\b|网络协议'),('software defined networking','软件定义网络',r'software.defined network|\bsdn\b'),('massive MIMO','大规模MIMO',r'massive mimo|\bmimo\b'),('reconfigurable intelligent surfaces','可重构智能表面',r'reconfigurable intelligent surface|intelligent reflecting surface'),('terahertz communications','太赫兹通信',r'terahertz.{0,50}(commun|wireless|channel)'),
('formal verification software','形式化验证',r'formal verif|model check|theorem prov|形式化验证'),('retrieval augmented generation','检索增强生成',r'retrieval.augmented|检索增强'),('AI agents','智能体',r'(autonomous|ai|llm|language model) agent|智能体'),('post quantum cryptography','后量子密码',r'post.quantum|lattice.based crypt|后量子密码'),('network architecture','网络体系结构',r'network architect|network slicing|网络架构'),
('compilers and programming languages','编译器与程序语言',r'compiler|programming language|static analy|编译器'),('storage and transaction systems','存储与事务系统',r'transaction|storage system|distributed storage|consistency model'),('containers and virtualization','容器与虚拟化',r'container orchestr|kubernetes|hypervisor|virtualiz'),('wireless channel coding','无线编码与波形',r'channel cod|ldpc|polar cod|ofdm|waveform'),('AI model evaluation and safety','模型评测与安全',r'(language model|llm|ai).{0,45}(safety|evaluat|align)|adversarial attack|jailbreak'),('software supply chain security','软件供应链安全',r'software supply.chain|dependency vulnerab|软件供应链')],
'新材料':[
('perovskite solar cells','钙钛矿光伏',r'perovskite.{0,55}(solar|photovoltaic)|钙钛矿太阳能'),('solid state batteries','固态电池',r'solid.state (batter|electrolyt)|固态电池'),('nanomaterials','纳米材料',r'nanomaterial|nanoparticle|nanocrystal|nanostructur|纳米材料'),('functional polymers','功能高分子',r'polymer|hydrogel|高分子'),('composite materials','复合材料',r'composite material|nanocomposite|fiber.reinforced|复合材料'),('lithium ion battery materials','锂离子电池',r'lithium.ion|锂离子'),('sodium ion batteries','钠离子电池',r'sodium.ion|钠离子'),('electrocatalysts','电催化',r'electrocatal|oxygen (evolution|reduction)|hydrogen evolution|电催化'),('photocatalytic materials','光催化',r'photocatal|光催化'),
('two dimensional materials','二维材料',r'two.dimensional material|2d material|graphene|二维材料'),('superconducting materials','超导',r'superconduct|超导'),('rare earth materials','稀土',r'rare.earth|lanthanide|稀土'),('high entropy alloys','高熵合金',r'high.entropy alloy|高熵合金'),('advanced ceramics','陶瓷',r'ceramic|陶瓷'),('materials informatics','材料信息学',r'materials informatic|materials.{0,30}machine learning|材料信息'),('computational materials design','计算材料设计',r'computational material|density functional|first.principles|材料计算'),('metal organic frameworks','金属有机框架',r'metal.organic framework|\bmofs?\b'),('biomaterials','生物材料',r'biomaterial|biocompatib|tissue scaffold|生物材料'),('self healing materials','自修复材料',r'self.healing|自修复'),('additive manufacturing alloys','增材制造金属',r'additive manufact|laser powder.bed|增材制造'),('thermoelectric materials','热电材料',r'thermoelectric|热电材料'),('ferroelectric materials','铁电材料',r'ferroelectric|铁电材料'),('hydrogen storage materials','储氢',r'hydrogen stor|metal hydride|储氢'),('optoelectronic materials','光电材料',r'optoelectronic|photodetect|light.emitting|光电材料'),
('structural metals and corrosion','结构金属与腐蚀',r'corrosion|fatigue|superalloy|structural steel|腐蚀'),('magnetic materials','磁性材料',r'ferromagnet|magnetic material|permanent magnet|磁性材料'),('membranes and separations','膜与分离材料',r'membrane.{0,40}(separat|filtrat|desalin)|separation material'),('materials scaleup and processing','材料制备与规模化',r'roll.to.roll|scale.up|large.scale synth|industrial manufact'),('recycling and sustainability','材料回收与循环',r'battery recycl|materials recycl|life.cycle assess|回收')],
'半导体与集成电路':[
('silicon photonics','硅光子',r'silicon photonic|photonic integrat|硅光子'),('chiplet integration','Chiplet集成',r'chiplet|芯粒'),('gallium nitride power devices','GaN功率器件',r'gallium nitride|\bgan\b|氮化镓'),('integrated circuit design','集成电路设计',r'integrated circuit|circuit design|集成电路设计'),('electronic design automation','EDA',r'electronic design automation|\beda\b|logic synthesis'),('semiconductor lithography','光刻',r'lithograph|光刻'),('advanced semiconductor packaging','先进封装',r'advanced packaging|semiconductor packaging|fan.out|先进封装'),('FinFET','FinFET',r'finfet'),('gate all around transistors','GAA',r'gate.all.around|nanosheet transistor'),('silicon carbide devices','SiC器件',r'silicon carbide|sic mosfet|碳化硅'),('memory chips','存储芯片',r'memory chip|\bdram\b|\bsram\b|\bnand\b|存储芯片'),('RRAM','RRAM',r'\brram\b|resistive (random|switch|memory)'),('MRAM','MRAM',r'\bmram\b|magnetic random.access|spin.transfer torque'),('RF integrated circuits','射频集成电路',r'rf.{0,20}(circuit|chip)|radio.frequency.{0,30}(circuit|integrat)|射频芯片'),('analog integrated circuits','模拟电路',r'analog.{0,35}(circuit|integrat)|\badc\b|\bdac\b|模拟电路'),('semiconductor reliability','半导体可靠性',r'(semiconductor|transistor|mosfet|chip).{0,50}(reliab|degrad|aging)|bias.temperature instability'),('semiconductor testing','芯片测试',r'(semiconductor|chip|circuit).{0,40}(test|fault)|design.for.test'),('VLSI physical design','物理设计',r'vlsi|physical design|routing|placement'),('neuromorphic chips','神经形态芯片',r'neuromorph|in.memory comput|神经形态'),('CMOS image sensors','CMOS图像传感器',r'cmos.{0,30}(imag|sensor)|image sensor'),('EUV lithography','EUV光刻',r'euv|extreme ultraviolet'),('semiconductor process metrology','工艺计量',r'(semiconductor|wafer|lithograph).{0,60}(metrolog|inspect)|critical.dimension'),('three dimensional integrated circuits','三维集成',r'three.dimensional integrat|3d integrat|through.silicon via'),('semiconductor materials epitaxy','半导体外延',r'epitax|molecular.beam|外延'),
('etch deposition and cleaning','刻蚀沉积清洗',r'(semiconductor|wafer|silicon|transistor).{0,60}(etch|deposit|clean)|atomic.layer deposit|刻蚀'),('yield and process control','良率与工艺控制',r'(wafer|chip|semiconductor).{0,50}(yield|process control)|statistical process control'),('HBM and advanced memory','HBM与先进存储',r'high.bandwidth memory|\bhbm\b'),('power management ICs','电源管理芯片',r'power.management|dc.dc convert|voltage regulator'),('hardware security','硬件安全',r'hardware secur|physical unclonable|side.channel attack'),('silicon manufacturing economics','制造成本与供应链',r'semiconductor.{0,60}(supply.chain|cost|econom)|foundry')]
}

# A declared editorial/technical selection, not JCR quartiles or a universal quality score.
GENERAL=['Nature','Science','Cell']
SELECTED={
'跨领域':['Nature Communications','Science Advances','Proceedings of the National Academy of Sciences','PNAS'],
'生物制药':['Nature Biotechnology','Nature Medicine','Nature Methods','Nature Immunology','Nature Genetics','Nature Reviews Drug Discovery','Science Translational Medicine','Science Immunology','Molecular Therapy','Journal of Controlled Release','Nucleic Acids Research','Genome Biology','Cell Stem Cell','Molecular Cancer','Acta Pharmaceutica Sinica B'],
'软件与通信':['IEEE Transactions on Pattern Analysis and Machine Intelligence','IEEE Transactions on Software Engineering','ACM Transactions on Computer Systems','ACM Transactions on Database Systems','IEEE Journal on Selected Areas in Communications','IEEE Transactions on Wireless Communications','IEEE Transactions on Information Theory','ACM Transactions on Graphics'],
'新材料':['Nature Materials','Nature Energy','Nature Nanotechnology','Nature Chemistry','Nature Catalysis','Advanced Materials','Advanced Functional Materials','Advanced Energy Materials','Energy & Environmental Science','ACS Nano','Nano Letters','Journal of the American Chemical Society','Angewandte Chemie International Edition','Chemical Reviews','Matter','Nano Energy','Energy Storage Materials','Chemistry of Materials'],
'半导体与集成电路':['IEEE Journal of Solid-State Circuits','IEEE Transactions on Electron Devices','IEEE Electron Device Letters','IEEE Transactions on Computer-Aided Design of Integrated Circuits and Systems','Nature Electronics','Nature Photonics','Light: Science & Applications','ACS Photonics','Optica','IEEE Transactions on Microwave Theory and Techniques']}
CONFERENCES={
'NeurIPS':r'^(advances in neural information processing systems|neurips|nips)$',
'ICML':r'^(proceedings of the .*international conference on machine learning|international conference on machine learning|icml)$',
'ICLR':r'^(international conference on learning representations|iclr)$',
'ACL':r'^(proceedings of the .*annual meeting of the association for computational linguistics.*|annual meeting of the association for computational linguistics|acl)$',
'EMNLP':r'^(proceedings of the .*conference on empirical methods in natural language processing.*|emnlp)$',
'CVPR':r'^.*(ieee cvf|ieee) conference on computer vision and pattern recognition.*$',
'ICCV':r'^.*international conference on computer vision.*$',
'ECCV':r'^.*european conference on computer vision.*$',
'SIGCOMM':r'^.*(acm sigcomm|special interest group on data communication).*$',
'SOSP':r'^.*symposium on operating systems principles.*$',
'OSDI':r'^.*symposium on operating systems design and implementation.*$',
'NSDI':r'^.*symposium on networked systems design and implementation.*$',
'USENIX Security':r'^.*usenix security symposium.*$',
'ACM CCS':r'^.*(acm sigsac conference on computer and communications security|acm conference on computer and communications security).*$',
'ISCA':r'^.*international symposium on computer architecture.*$',
'MICRO':r'^.*(international symposium on microarchitecture|microarchitecture micro).*$',
'ASPLOS':r'^.*architectural support for programming languages and operating systems.*$',
'PLDI':r'^.*programming language design and implementation.*$',
'POPL':r'^.*principles of programming languages.*$',
'DAC':r'^.*design automation conference.*$',
'ICCAD':r'^.*international conference on computer aided design.*$',
'ISSCC':r'^.*international solid state circuits conference.*$',
'IEDM':r'^.*international electron devices meeting.*$',
'VLSI Symposium':r'^.*symposium on vlsi (technology|circuits|technology and circuits).*$',
}

SELECTED['跨领域']+=['Advanced Science','National Science Review']
SELECTED['生物制药']+=['Nature Biomedical Engineering','Cell Reports Medicine']
SELECTED['软件与通信']+=['Nature Machine Intelligence','IEEE Transactions on Communications','IEEE Communications Surveys & Tutorials','Journal of Lightwave Technology']
SELECTED['新材料']+=['Nano-Micro Letters','npj Computational Materials','Nano Research','Small','Joule','Applied Catalysis B: Environmental']
SELECTED['半导体与集成电路']+=['Microsystems & Nanoengineering']
CONFERENCES.update({
'AAAI':r'^.*(aaai conference on artificial intelligence).*$',
'IJCAI':r'^.*international joint conference on artificial intelligence.*$',
'KDD':r'^.*(knowledge discovery and data mining|acm sigkdd).*$',
'SIGIR':r'^.*(international acm sigir|research and development in information retrieval).*$',
'SIGMOD':r'^.*(acm sigmod|management of data).*$',
'VLDB':r'^.*(very large data bases|vldb).*$',
'ICDE':r'^.*international conference on data engineering.*$',
'ICSE':r'^.*international conference on software engineering.*$',
'FSE':r'^.*(foundations of software engineering|esec fse).*$',
'ASE':r'^.*automated software engineering.*$',
'CAV':r'^.*computer aided verification.*$',
'IEEE S&P':r'^.*ieee symposium on security and privacy.*$',
'INFOCOM':r'^.*(ieee infocom|conference on computer communications).*$',
'ASP-DAC':r'^.*asia and south pacific design automation conference.*$',
'DATE':r'^.*design automation.{0,10}test in europe.*$',
'EuroSys':r'^.*european conference on computer systems.*$',
'FAST':r'^.*conference on file and storage technologies.*$',
'USENIX ATC':r'^.*usenix annual technical conference.*$',
'HPCA':r'^.*international symposium on high performance computer architecture.*$',
'SC':r'^.*international conference for high performance computing networking storage and analysis.*$',
'PPoPP':r'^.*principles and practice of parallel programming.*$',
'NDSS':r'^.*network and distributed system security symposium.*$',
'CRYPTO':r'^.*(annual international cryptology conference|advances in cryptology crypto).*$',
'EUROCRYPT':r'^.*advances in cryptology eurocrypt.*$',
})
