"""One-time approved taxonomy migration; definitions stay in configuration."""
import csv,json,re,pathlib,copy
from corpus_store import ROOT,STATE,load_config,csvwrite,atomic_json,now

# id, Chinese name, parent, aliases, concrete English/Chinese technical topics
DEFINITIONS = [
('robotics_manufacturing','机器人与智能制造','industrial_technology','机器人与智能制造|高端装备制造|Mechanical Engineering|Engineering','robot motion planning/机器人运动规划;robot manipulation/机器人操作;industrial robotics/工业机器人;digital twin manufacturing/制造数字孪生;machine tool machining/机床加工;predictive maintenance/预测性维护'),
('energy_storage','储能与电池','industrial_technology','储能与电池','lithium ion batteries/锂离子电池;solid state batteries/固态电池;sodium ion batteries/钠离子电池;redox flow batteries/液流电池;battery management systems/电池管理系统'),
('renewable_energy','新能源与光伏','industrial_technology','新能源与光伏|新能源|光伏','perovskite solar cells/钙钛矿太阳能电池;silicon photovoltaics/硅光伏;wind turbine control/风机控制;solar thermal energy/太阳能热利用'),
('hydrogen_energy','氢能','industrial_technology','氢能','water electrolysis/电解水制氢;hydrogen storage/储氢;hydrogen fuel cells/氢燃料电池;hydrogen production/制氢'),
('aerospace_unmanned','航空航天与无人系统','industrial_technology','航空航天与无人系统|航空航天|低空经济与无人系统','unmanned aerial vehicles/无人机;spacecraft propulsion/航天推进;satellite navigation/卫星导航;aircraft aerodynamics/飞行器空气动力学;urban air mobility/城市空中交通'),
('medical_precision','医疗器械与精准医疗','industrial_technology','医疗器械与精准医疗|医疗器械|精准医疗|Medicine|Health Sciences|Biomedical Engineering|Pathology','medical imaging/医学成像;surgical robotics/手术机器人;precision medicine/精准医疗;liquid biopsy/液体活检;digital pathology/数字病理;implantable medical devices/植入式医疗器械'),
('synthetic_biology','合成生物学','industrial_technology','合成生物学','synthetic biology/合成生物学;metabolic engineering/代谢工程;microbial cell factories/微生物细胞工厂;genetic circuits/基因线路'),
('chemical_catalysis','绿色化工与催化','industrial_technology','绿色化工与催化|化工与绿色化工|催化|Chemistry|Chemical engineering','green chemistry/绿色化学;heterogeneous catalysis/多相催化;electrocatalysis/电催化;photocatalysis/光催化;chemical process intensification/化工过程强化'),
('environment_carbon','碳捕集与环保','industrial_technology','碳捕集与环保|节能环保|碳捕集与碳中和技术','carbon capture/碳捕集;direct air capture/直接空气捕集;carbon dioxide utilization/二氧化碳利用;wastewater treatment/废水处理;industrial waste heat recovery/工业余热回收'),
('photonics_optics','光电子与激光工程','industrial_technology','光电子与激光工程|光电子|激光与光学工程|Optoelectronics|Optics|Photonics|Photonic and Optical Devices','silicon photonics/硅光子;integrated photonics/集成光子;semiconductor lasers/半导体激光器;laser machining/激光加工;photonic crystals/光子晶体;optical metasurfaces/光学超表面'),
('quantum_technology','量子科技','industrial_technology','量子科技|Quantum mechanics','quantum computing/量子计算;quantum communication/量子通信;quantum sensing/量子传感;quantum error correction/量子纠错;quantum entanglement/量子纠缠'),
('sensors_mems','先进传感器与MEMS','industrial_technology','先进传感器与MEMS|先进传感器|MEMS','microelectromechanical systems/微机电系统;MEMS sensors/MEMS传感器;inertial sensors/惯性传感器;flexible sensors/柔性传感器;optical biosensors/光学生物传感器'),
('cybersecurity','网络安全','industrial_technology','网络安全','intrusion detection/入侵检测;network security/网络安全;post quantum cryptography/后量子密码;software vulnerability detection/软件漏洞检测;secure multiparty computation/安全多方计算'),
('agricultural_technology','农业科技','industrial_technology','农业科技','precision agriculture/精准农业;crop phenotyping/作物表型;agricultural robotics/农业机器人;genomic selection breeding/基因组选择育种;smart irrigation/智能灌溉'),
('marine_engineering','海洋工程','industrial_technology','海洋工程','autonomous underwater vehicles/自主水下航行器;offshore structures/海洋平台结构;wave energy converters/波浪能转换;underwater acoustic communication/水声通信;marine corrosion protection/海洋防腐'),
('additive_manufacturing','增材制造与先进工艺','industrial_technology','增材制造|先进制造工艺','additive manufacturing/增材制造;laser powder bed fusion/激光粉末床熔融;directed energy deposition/定向能量沉积;friction stir welding/搅拌摩擦焊;precision machining/精密加工'),
('transport_electric_vehicles','智能交通与新能源汽车','industrial_technology','智能交通与新能源汽车|新能源汽车|智能交通','electric vehicles/电动汽车;autonomous driving/自动驾驶;vehicle to grid/车网互动;intelligent transportation systems/智能交通系统;electric vehicle charging/电动汽车充电'),
('industrial_digital','工业软件与数据基础设施','industrial_technology','工业软件|数据基础设施|Information Systems','industrial software/工业软件;computer aided design/计算机辅助设计;manufacturing execution systems/制造执行系统;data infrastructure/数据基础设施;data lakehouse/数据湖仓;enterprise information systems/企业信息系统'),
('smart_energy','智慧能源','industrial_technology','智慧能源','smart grids/智能电网;microgrid control/微电网控制;energy management systems/能源管理系统;demand response/需求响应;power system optimization/电力系统优化'),
('display_technology','新型显示','industrial_technology','新型显示','micro LED displays/微型LED显示;organic light emitting diodes/有机发光二极管;quantum dot displays/量子点显示;flexible displays/柔性显示'),
('electrical_engineering','电气与电子工程','engineering_sciences','Electrical and Electronic Engineering|Electrical engineering|Electronic engineering','power electronics/电力电子;electric motor control/电机控制;radio frequency circuits/射频电路;analog integrated circuits/模拟集成电路'),
('physical_sciences','物理与工程物理','physical_sciences','Physical Sciences|Physics|Physics and Astronomy|Engineering physics','condensed matter physics/凝聚态物理;plasma physics/等离子体物理;particle detectors/粒子探测器;computational astrophysics/计算天体物理;statistical mechanics/统计力学'),
('life_sciences','生命科学与分子生物学','life_sciences','Life Sciences|Biology|Biochemistry, Genetics and Molecular Biology|Molecular Biology|Genetics|Gene|Biochemistry|Immunology|Computational biology','gene regulation/基因调控;genome editing/基因组编辑;protein structure/蛋白质结构;single cell sequencing/单细胞测序;enzyme kinetics/酶动力学;immune receptor signaling/免疫受体信号;computational biology/计算生物学'),
('mathematics','数学与计算方法','mathematical_sciences','Mathematics','numerical optimization/数值优化;partial differential equations/偏微分方程;stochastic differential equations/随机微分方程;topological data analysis/拓扑数据分析'),
('social_business','社会科学与商业创新','social_sciences','Social Sciences|Business','innovation policy/创新政策;entrepreneurship ecosystems/创业生态;research commercialization/科研商业化;technology adoption/技术采纳;science policy/科技政策'),
('materials_support','材料化学与纳米冶金','physical_sciences','Nanotechnology|Materials Chemistry|Metallurgy|Perovskite Materials and Applications|Perovskite (structure)|Composite material','perovskite crystal structures/钙钛矿晶体结构;composite materials/复合材料;nanofabrication/纳米加工;nanomaterials synthesis/纳米材料合成;alloy metallurgy/合金冶金;polymer chemistry/高分子化学'),
]

def pattern(term):
    escaped=re.escape(term).replace(r'\ ',r'[\s\-\u2010-\u2015]+')
    # Accept singular/plural technical phrases while retaining all other words.
    if term.endswith('s'):escaped=escaped[:-1]+'s?'
    return r'\b'+escaped+r'\b'

def expand():
    cfg=load_config();old=copy.deepcopy(cfg['domains']);lookup={}
    for d in old:lookup[d['domain_name'].casefold()]=(d['domain_id'],None)
    for ident,cn,parent,aliases,topics in DEFINITIONS:
        pairs=[x.split('/') for x in topics.split(';')];ts=[]
        for en,zh in pairs:ts.append(dict(topic_id=en,name=en,aliases=[zh],queries=[en,zh],positive_terms=[pattern(en),zh],negative_terms=[],priority=1,importance=1,emergence=1,transfer_relevance=1,target_depth=30))
        biomedical=parent=='life_sciences' or ident in ['medical_precision','synthetic_biology','agricultural_technology']
        sources=['europe_pmc','openalex','crossref'] if biomedical else ['arxiv','openalex','crossref']
        venues=['Nature Communications','Scientific Reports']+(['Nature Biotechnology','Genome Biology'] if biomedical else ['Science Advances','IEEE Access'])
        d=dict(domain_id=ident,domain_name=cn,domain_name_cn=cn,domain_name_en=ident.replace('_',' ').title(),parent_domain=parent,aliases=aliases.split('|'),enabled=True,approval='explicit_user_2026-10-07',description=cn+'：'+ '；'.join(zh for en,zh in pairs)+'。须有具体技术核心和方法/机制上下文，来源学科标签不作为核心证据。',topics=ts,positive_terms=[x for t in ts for x in t['positive_terms']],context_terms=[r'method|mechanis|design|develop|fabricat|synthes|experiment|perform|characteriz|control|optim|engineer|comput|regulat|detect|measur|system|device|algorithm|technolog|应用|设计|机制|方法|制备|性能|实验|优化|调控']+[pattern(en) for en,zh in pairs],negative_terms=[r'primary school assessment|astrological prediction|horoscope'],weak_terms=[],boundary_terms=[],negative_exceptions=[],preferred_sources=sources,search_sources=sources,preferred_venues=venues,oa_resolvers=['openalex','semantic_scholar','europe_pmc'],priority=1,importance=1,transfer_relevance=1,target_depth=300,target_count=300,target_years=list(range(2019,2027)),year_policy=copy.deepcopy(old[0]['year_policy']),query_terms_en=[en for en,zh in pairs],query_terms_cn=[zh for en,zh in pairs])
        cfg['domains'].append(d)
        for alias in aliases.split('|'):lookup[alias.casefold()]=(ident,None)
    explicit={
      'Computer science':('software_communications',None),'人工智能与计算机科学':('software_communications',None),'Artificial Intelligence':('software_communications',['machine learning','large language models']),
      'Materials science':('advanced_materials',None),'Operating system':('software_communications',['operating systems']), 'Programming language':('software_communications',['software engineering','formal verification software']),
      'Computer Networks and Communications':('software_communications',['network protocols','network architecture','5G networks','6G wireless communications']), 'Telecommunications':('software_communications',['5G networks','6G wireless communications','optical communications']),
      '云计算与边缘计算':('software_communications',['cloud computing','edge computing']),'5G/6G':('software_communications',['5G networks','6G wireless communications']),'物联网':('software_communications',['internet of things']),
      'RNA Interference and Gene Delivery':('biopharmaceuticals',['RNA delivery','gene therapy','lipid nanoparticle delivery']),
      'Gene':('life_sciences',['gene regulation','genome editing']), 'Genetics':('life_sciences',['gene regulation','genome editing','single cell sequencing']), 'Immunology':('life_sciences',['immune receptor signaling']), 'Computational biology':('life_sciences',['computational biology']),
      'Pathology':('medical_precision',['digital pathology','liquid biopsy']), 'Perovskite (structure)':('materials_support',['perovskite crystal structures']), 'Perovskite Materials and Applications':('materials_support',['perovskite crystal structures']), 'Composite material':('materials_support',['composite materials']), 'Metallurgy':('materials_support',['alloy metallurgy']),
      'MEMS':('sensors_mems',['microelectromechanical systems','MEMS sensors']), '光伏':('renewable_energy',['perovskite solar cells','silicon photovoltaics']), '新型显示':('display_technology',None), '催化':('chemical_catalysis',['heterogeneous catalysis','electrocatalysis','photocatalysis'])}
    lookup.update({k.casefold():v for k,v in explicit.items()})
    approval=json.loads((ROOT/'NEXT_CHAT_APPROVED_DOMAIN_SCOPE.json').read_text(encoding='utf8'))
    proposals=list(csv.DictReader((ROOT/'DOMAIN_EXPANSION_PROPOSAL.csv').open(encoding='utf-8-sig')))
    names=[d['domain_name'] for d in approval['existing_domains']]+[d['proposed_domain'] for d in approval['approved_proposal_rows']]+approval['approved_industrial_scope']+[d['proposed_domain'] for d in proposals]
    rows=[];domains={d['domain_id']:d for d in cfg['domains']}
    for name in dict.fromkeys(names):
        if name.casefold() not in lookup:raise ValueError('Unmapped approved scope: '+name)
        ident,selected=lookup[name.casefold()];d=domains[ident];ts=[t for t in d['topics'] if selected is None or t['topic_id'] in selected]
        assert ts
        rows.append(dict(original_name=name,canonical_domain_id=ident,domain_name=d['domain_name'],parent_family=d['parent_domain'],mapped_topic=[t['topic_id'] for t in ts],aliases=d['aliases'],enabled=True,search_strategy=dict(sources=d['preferred_sources'],years=d['target_years'],priority_years=[2023,2024],queries=[t['queries'] for t in ts],bounded_frontier=True,legal_OA_only=True)))
    assert cfg['domains'][:len(old)]==old
    cfg['scope_mapping_file']='APPROVED_DOMAIN_SCOPE_MAP.csv';cfg['approval_expansion_at']=now();cfg['semantic']['enabled']=False;cfg['semantic']['optional']=True
    cfg['continuous']['fair_dispatch']=True;cfg['continuous']['fair_scope_mapping']='APPROVED_DOMAIN_SCOPE_MAP.csv'
    atomic_json(STATE/'corpus_config.json',cfg);csvwrite(ROOT/'APPROVED_DOMAIN_SCOPE_MAP.csv',rows)
    atomic_json(STATE/'approved_scope_registry.json',dict(approval=approval,execution_proposal_names=[d['proposed_domain'] for d in proposals],rows=rows,at=now()))
    print(json.dumps(dict(mapped=len(rows),proposal_rows=len(proposals),domains=len(cfg['domains']),families=len({d['parent_domain'] for d in cfg['domains']}),topics=sum(len(d['topics']) for d in cfg['domains'])),ensure_ascii=False))

if __name__=='__main__':expand()
