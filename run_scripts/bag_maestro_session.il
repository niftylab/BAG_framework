; Maestro (ADE Assembler) session function family for the BAG skill server.
;
; ADE Assembler ("maestro") is the successor to ADE-XL and drives the same
; axl* SKILL API, so these entry points reuse the ADE-XL functions in
; bag_adexl_session.il with the "maestro" cellview instead of "adexl".  The
; maestro view (maestro.sdb + active.state) is authored in Virtuoso and
; opened in place; there is no separate instantiate step.
;
; As with ADE-XL, the simulation itself runs through the Ocean simulation
; interface (bag.interface.ocean.OceanInterface), not the skill server -- these
; functions only read and update the assembler setup database.
;
; Split out of start_bag_ICADV12d3.il like the other flavor families
; (cf. bag_adexl_session.il / bag_adel_session.il); shared helpers stay in the
; main file, which loads this file before starting the BAG server.

; Returns parameter and corner information of a maestro testbench.
procedure( maestro_get_testbench_info(tb_lib tb_cell result_file "ttt")
    get_testbench_info(tb_lib tb_cell result_file "maestro")
)

; Modify corners, parameters, and run options of a maestro testbench.
procedure( maestro_modify_testbench(tb_lib tb_cell conf_file opt_file corner_file
                                    param_file env_params_file "ttttttt")
    modify_testbench(tb_lib tb_cell conf_file opt_file corner_file param_file
                     env_params_file "maestro")
)
