; ADE-XL session function family for the BAG skill server.
;
; Split out of start_bag_ICADV12d3.il so each ADE flavor's testbench
; functions live in their own file (cf. bag_adel_session.il; a Maestro family will
; follow the same pattern). Shared helpers -- copy_testbench,
; create_config_view, edit_config_view, parse_data_from_file, ... --
; remain in start_bag_ICADV12d3.il, which loads this file before starting
; the BAG server.

; opens an adexl session.  Returns a list of session name and setup database handle.
procedure( open_adexl_session(tb_lib tb_cell tb_view session_name mode "ttttt")
    let( (session sdb)
        unless( session = axlCreateSession(session_name)
            error("Cannot create temporary adexl session: %s" session_name)
        )
        unless( sdb = axlSetMainSetupDBLCV(session tb_lib tb_cell tb_view ?mode mode)
            axlCloseSession(session)
            error("Cannot load adexl database from %s__%s (%s)" tb_lib tb_cell tb_view)
        )
        list(session sdb)
    )
)

; Enables only the given corners in the simulation setup database.
procedure( enable_adexl_corners( sdb corner_list env_param_list "gll")
    let( (env_name par_val_list corner)
        foreach(cur_name cadr(axlGetCorners(sdb))
            axlSetEnabled( axlGetCorner(sdb cur_name) member(cur_name corner_list) )
        )
        foreach(env_par_obj env_param_list
            env_name = car(env_par_obj)
            par_val_list = cadr(env_par_obj)
            corner = axlGetCorner(sdb env_name)
            foreach(par_val par_val_list
                axlPutVar(corner car(par_val) cadr(par_val))
            )
        )
    )
)

; Set testbench parameters
; val_list is an association list from variable names to variable values as string, which
; could be a constant value or a parametric sweep string
procedure( set_adexl_parameters(sdb par_val_list "gl")
    foreach( var_spec par_val_list
        axlPutVar(sdb car(var_spec) cadr(var_spec))
    )
)

; Write testbench information to file.
procedure( write_testbench_info_to_file(sdb result_file output_list en_corner_list)
    let( (p output_count)

        ; write testbench information to result_file
        p = outfile(result_file "w")

        fprintf(p "corners:\n")
        foreach( corn cadr(axlGetCorners(sdb))
            fprintf(p "  - %s\n" corn)
        )
        fprintf(p "enabled_corners:\n")
        foreach( corn en_corner_list
            fprintf(p "  - %s\n" corn)
        )
        fprintf(p "parameters:\n")
        if( var_list = cadr(axlGetVars(sdb)) then
            foreach( var_name var_list
                fprintf(p "  %s: \"%s\"\n" var_name axlGetVarValue(axlGetVar(sdb var_name)))
            )
        else
            fprintf(p "  {}\n")
        )
        fprintf(p "outputs:\n")
        output_count = 0
        foreach( out_obj output_list
            if( rexMatchp( "\"" out_obj->name) then
                warn("Output expression name (%s) have quotes, skipping" out_obj->name)
            else
                fprintf(p "  \"%s\": !!str %A\n" out_obj->name out_obj->expression)
                output_count = output_count + 1
            )
        )
        when( output_count == 0
            fprintf(p "  {}\n")
        )
        close(p)
    )
)

; Instantiates a testbench.
;
; Copy a testbench template to the desired location, replace instances, make config view,
; and also setup corner settings in adexl.
; this method will also record list of corners, global variables, and output expressions
; to result_file
procedure( instantiate_testbench(tb_cell targ_lib
                                 config_libs config_views config_stops
                                 default_corner corner_file def_files
                                 tech_lib result_file
                                 "tttttttltt")
    let( (session_name session_sdb session sdb test_names test_name test tool_args corner_list
          ade_symbol ade_session output_list tmp_state_name state_obj success)

        tmp_state_name = "orig_state"

        ; check if temporary ADE session state already exists, if so, delete it
        state_obj = ddGetObj(targ_lib tb_cell tmp_state_name)
        when( state_obj
            success = ddDeleteObj(state_obj)
            unless( success
                error("Cannot delete orig_state cellview.")
            )
        )

        ; create config view
        create_config_view(targ_lib tb_cell "config" config_libs config_views config_stops)

        ; session_name = "modify_adexl"
        session_name = sprintf(nil "modify_adexl_%d" bag_modify_adexl_counter)
        bag_modify_adexl_counter = bag_modify_adexl_counter + 1

        session_sdb = open_adexl_session(targ_lib tb_cell "adexl" session_name "a")
        session = car(session_sdb)
        sdb = cadr(session_sdb)

        ; check that only one test is defined
        test_names = cadr(axlGetTests(sdb))
        when(length(test_names) != 1
            axlCommitSetupDB(sdb)
            axlCloseSetupDB(sdb)
            axlCloseSession(session)
            error("ADEXL testbench must have exactly 1 test defined.")
        )

        ; save current test setup state
        axlSaveSetupState(session "adexl_default" "All")

        ; change all tests to use config view, and set all test's definition files
        ; also get a list of defined output expressions
        ; step 1: get ADE session
        test_name = car(test_names)
        ade_symbol = axlGetToolSession(session_name test_name)
        ade_session = asiGetSession(ade_symbol)
        ; step 2: save original ADE session
        asiSaveState(ade_session ?name tmp_state_name ?option 'cellview ?lib targ_lib ?cell tb_cell)
        ; step 3: change test library
        test = axlGetTest(sdb test_name)
        tool_args = axlGetTestToolArgs(test)
        set_assoc_list(tool_args "view" "config")
        set_assoc_list(tool_args "lib" targ_lib)
        set_assoc_list(tool_args "cell" tb_cell)
        axlSetTestToolArgs(test tool_args)
        ; step 4: reopen ADE session, then load original ADE state
        ade_symbol = axlGetToolSession(session_name test_name)
        ade_session = asiGetSession(ade_symbol)
        asiLoadState(ade_session ?name tmp_state_name ?option 'cellview)
        asiSetEnvOptionVal(ade_session 'definitionFiles def_files)
        output_list = setof(ele asiGetOutputList(ade_session) ele->name)
        ; step 5: delete temporary ADE session state
        state_obj = ddGetObj(targ_lib tb_cell tmp_state_name)
        ddDeleteObj(state_obj)

        axlMainAppSaveSetup(session_name)

        ; load corner
        unless(axlLoadCorners(sdb corner_file)
            axlCommitSetupDB(sdb)
            axlCloseSetupDB(sdb)
            axlCloseSession(session)
            error("Error loading corner file %s to %s__%s (%s)" corner_file lib_name cell_name view_name)
        )

        ; set default corner
        corner_list = list(default_corner)
        enable_adexl_corners(sdb corner_list nil)

        ; write testbench information to file
        write_testbench_info_to_file(sdb result_file output_list corner_list)

        ; save and close
        axlSaveSetupState(session "adexl_default" "All")
        axlSaveSetupState(session "ocean_default" "All")
        axlMainAppSaveSetup(session_name)
        axlCommitSetupDB(sdb)
        axlCloseSetupDB(sdb)
        axlCloseSession(session)
    )
)

; Returns parameter and corner information of a testbench.
procedure( get_testbench_info(tb_lib tb_cell result_file "ttt")
    let( (session_name session_sdb session sdb test_names test_name ade_symbol asi_sess
        output_list corner_list en_list success)
        session_name = "read_adexl"
        session_sdb = open_adexl_session(tb_lib tb_cell "adexl" session_name "r")
        session = car(session_sdb)
        sdb = cadr(session_sdb)

        ; check that only one test is defined
        test_names = cadr(axlGetTests(sdb))
        when(length(test_names) != 1
            axlCommitSetupDB(sdb)
            axlCloseSetupDB(sdb)
            axlCloseSession(session)
            error("ADEXL testbench must have exactly 1 test defined.")
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

        ; close
        axlCommitSetupDB(sdb)
        axlCloseSetupDB(sdb)
        axlCloseSession(session)
    )
)

; Configure run options.  Used to setup monte carlo parameters.
; run_params is an association list of run options and their values.  The key "mode"
; corresponds to the run mode.
procedure( set_run_options(session sdb run_params "ggl")
    let( (run_mode opt_list run_opt)
        when( run_mode = cadr(assoc("mode" run_params))
                  ; no options for single run/sweep mode.
            cond( (run_mode == "Single Run, Sweeps and Corners"
                   opt_list = nil)
                  (run_mode == "Monte Carlo Sampling"
                   opt_list = '("mcnumpoints" "mcmethod") )
                  ('t
                      axlCloseSession(session)
                      error("Unsupported run mode: %s" run_mode) )
            )
            foreach( opt_name opt_list
                when( opt_val = cadr(assoc(opt_name run_params))
                    run_opt = axlPutRunOption(sdb run_mode opt_name)
                    axlSetRunOptionValue(run_opt opt_val)
                )
            )
            axlSetCurrentRunMode(sdb run_mode)
        )
    )
)

; modify the given testbench.
; tb_lib and tb_cell describes the library and cell of the testbench to simulate.
; conf_file contains the config view settings.
; opt_file contains the association list of run mode options.
; corner_file contains a list of corners to simulate.
; param_file contains the association list of parameter values.
procedure( modify_testbench(tb_lib tb_cell conf_file opt_file corner_file param_file env_params_file "ttttttt")
    let( (tmp_list session sdb conf_list run_params corner_list param_values env_param_values session_name)
        sprintf(session_name "bag_sim_adexl_%s" getCurrentTime())

        ; read inputs from file.
        conf_list = parse_data_from_file(conf_file)
        run_params = parse_data_from_file(opt_file)
        corner_list = parse_data_from_file(corner_file)
        param_values = parse_data_from_file(param_file)
        env_param_values = parse_data_from_file(env_params_file)

        ; modify config view
        when( conf_list
            edit_config_view(tb_lib tb_cell "config" conf_list)
        )

        tmp_list = open_adexl_session(tb_lib tb_cell "adexl" session_name "a")
        session = car(tmp_list)
        sdb = cadr(tmp_list)

        ; change corners, parameters, and run options
        enable_adexl_corners( sdb corner_list env_param_values)
        set_adexl_parameters( sdb param_values )
        set_run_options( session sdb run_params )

        ; save and close
        axlSaveSetupState(session "adexl_default" "All")
        axlSaveSetupState(session "ocean_default" "All")
        axlMainAppSaveSetup(session_name)
        axlCommitSetupDB(sdb)
        axlCloseSetupDB(sdb)
        axlCloseSession(session)
    )
)
