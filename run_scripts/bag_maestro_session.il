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
;
; Unlike the ADE-XL get_testbench_info this NEVER writes to the setup
; database: the session opens read-only and closes without
; axlCommitSetupDB, because setupdb write operations on maestro views
; crash IC618 (SDB::setCurrentRunMode assertion, observed 2026-08-09).
procedure( maestro_get_testbench_info(tb_lib tb_cell result_file "ttt")
    let( (session_name session_sdb session sdb test_names test_name ade_symbol asi_sess
        output_list corner_list en_list)
        session_name = "read_maestro"
        session_sdb = open_adexl_session(tb_lib tb_cell "maestro" session_name "r")
        session = car(session_sdb)
        sdb = cadr(session_sdb)

        ; check that only one test is defined
        test_names = cadr(axlGetTests(sdb))
        when(length(test_names) != 1
            axlCloseSetupDB(sdb)
            axlCloseSession(session)
            error("Maestro testbench must have exactly 1 test defined.")
        )

        ; get output list
        test_name = car(test_names)
        ade_symbol = axlGetToolSession(session_name test_name)
        asi_sess = sevEnvironment(ade_symbol)
        output_list = setof(ele asiGetOutputList(asi_sess) ele->name)

        ; get enabled corners
        corner_list = cadr(axlGetCorners(sdb))
        en_list = setof(corner corner_list axlGetEnabled(axlGetCorner(sdb corner)))

        ; write testbench information to file
        write_testbench_info_to_file(sdb result_file output_list en_list)

        ; close read-only (no commit)
        axlCloseSetupDB(sdb)
        axlCloseSession(session)
    )
)

; Modify corners, parameters, and run options of a maestro testbench.
;
; WARNING: do not call this on IC618 -- the axl write path
; (axlSaveSetupState & friends) crashes setupdb on maestro views
; (SDB::setCurrentRunMode assertion, observed 2026-08-09).  Maestro
; setups run exactly as saved; edit the maestro view in Virtuoso instead.
; Kept for newer Virtuoso versions where the axl write path may be safe.
procedure( maestro_modify_testbench(tb_lib tb_cell conf_file opt_file corner_file
                                    param_file env_params_file "ttttttt")
    modify_testbench(tb_lib tb_cell conf_file opt_file corner_file param_file
                     env_params_file "maestro")
)
