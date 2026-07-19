* Annotated CDL hierarchical BAG template

.SUBCKT inv IN OUT VDD VSS PARAMS: wn=1u wp=2u lch=45n nf=2
* @BAG {"lib_name":"logic_templates"}
*.PININFO IN:I OUT:O VDD:B VSS:B

MN0 OUT IN VSS VSS nmos4_lvt
+ w='wn' l='lch' nf='nf' $ @BAG {"lib_name":"BAG_prim"}
MP0 OUT IN VDD VDD pmos4_lvt w='wp' l='lch' nf='nf' $ ordinary comment
+ m=1 $ @BAG {"lib_name":"BAG_prim"}

.ENDS inv

.SUBCKT buffer2 IN OUT VDD VSS PARAMS: wn=1u wp=2u lch=45n nf=2
* ordinary cell comment
* @BAG {"lib_name":"logic_templates"}
*.PININFO IN:I OUT:O VDD:B VSS:B

XINV0 IN MID VDD VSS inv wn='wn' wp='wp' lch='lch' nf='nf' $ @BAG {"lib_name":"logic_templates"}
XINV1 MID OUT VDD VSS inv wn='2*wn' wp='2*wp' lch='lch' nf='nf' $ @BAG {"lib_name":"logic_templates"}

.ENDS buffer2
