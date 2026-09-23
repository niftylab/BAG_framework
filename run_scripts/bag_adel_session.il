; ADE-L session function family for the BAG skill server.
;
; Split out of start_bag_ICADV12d3.il so each ADE flavor's testbench
; functions live in their own file (cf. bag_adexl_session.il; a Maestro family will
; follow the same pattern). Shared helpers -- copy_testbench,
; create_config_view, edit_config_view, parse_data_from_file, ... --
; remain in start_bag_ICADV12d3.il, which loads this file before starting
; the BAG server.

; opens an adel session.  Returns a list of session name and setup database handle.
procedure( open_adel_session(tb_lib tb_cell tb_view tb_ade_view session_name mode "tttttt")
    let( (sev_session wid session)
        sev_session = sevStartSession(?lib tb_lib ?cell tb_cell ?view tb_view)
        wid = hiGetCurrentWindow()
        session = asiGetSession(wid)
        asiLoadState(session ?name tb_ade_view ?option 'cellview ?lib tb_lib ?cell tb_cell)
        list(sev_session session)
    );let
)

; (ADE-L) Set testbench parameters
; val_list is an association list from variable names to variable values as string, which
; could be a constant value or a parametric sweep string
procedure( set_adel_parameters(session par_val_list "gl")
    asiSetDesignVarList(session par_val_list)
)

; (ADE-L) Write testbench information to file.
procedure( adel_write_testbench_info_to_file(session result_file output_list corner_file en_corner_list)
    let( (p output_count)

        ; write testbench information to result_file
        p = outfile(result_file "w")

        fprintf(p "corner_file:\n")
        fprintf(p "  - %s\n" corner_file)

        fprintf(p "corners:\n")
        foreach( corn cdar(asiGetModelLibSelectionList(session))
            fprintf(p "  - %s\n" corn)
        )
        
        fprintf(p "enabled_corners:\n")
        foreach( corn en_corner_list
            fprintf(p "  - %s\n" corn)
        )
        fprintf(p "parameters:\n")
        if( var_list = asiGetDesignVarList(session) then 
            foreach( var var_list
                fprintf(p "  %s: \"%s\"\n" car(var) cadr(var))
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

; (ADE-L) Instantiates a testbench.
;
; Copy a testbench template to the desired location, replace instances, make config view,
; and also setup corner settings in adexl.
; this method will also record list of corners, global variables, and output expressions
; to result_file
procedure( adel_instantiate_testbench(tb_cell targ_lib
                                 config_libs config_views config_stops
                                 default_corner corner_file def_files
                                 tech_lib result_file @optional (tb_ade_view "spectre_state1")
                                 "tttttttltt")
    let( (session_name sev_sess_sess session sev_session corner_list output_list state_obj success)

        ; create config view
        create_config_view(targ_lib tb_cell "config" config_libs config_views config_stops)

        ; open an ADE session
        sprintf(session_name "modify_adel_%s" getCurrentTime())
        sev_sess_sess = open_adel_session(targ_lib tb_cell "config" tb_ade_view session_name "a")
        sev_session = car(sev_sess_sess)
        session = cadr(sev_sess_sess)

        ; read outputs
        output_list = setof(ele asiGetOutputList(session) ele->name)
        
        ; set default corner
        corner_list = list(default_corner)

        ; write testbench information to file
        adel_write_testbench_info_to_file(session result_file output_list corner_file corner_list)

        ; close
        sevQuit(sev_session)
    );let
)

; (ADE-L) modify the given testbench.
; tb_lib and tb_cell describes the library and cell of the testbench to simulate.
; conf_file contains the config view settings.
; opt_file contains the association list of run mode options.
; corner_file contains a list of corners to simulate.
; param_file contains the association list of parameter values.
; stim_file, when non-empty, contains a BAG stimulus spec (see
; bag_apply_stimuli in bag_adexl_session.il).
procedure( adel_modify_testbench(tb_lib tb_cell conf_file opt_file corner_file sim_env_file param_file
                                 env_params_file @optional (tb_ade_view "spectre_state1") (stim_file "") "ttttttt")
    ;let( (sev_sess_sess sev_session session sdb conf_list run_params corner_list env_param_values session_name)
        sprintf(session_name "bag_sim_adel_%s" getCurrentTime())

        ; read inputs from file.
        conf_list = parse_data_from_file(conf_file)
        run_params = parse_data_from_file(opt_file)
        ;corner_file
        corner_list = parse_data_from_file(sim_env_file)
        param_values = parse_data_from_file(param_file)
        env_param_values = parse_data_from_file(env_params_file)
        stim_spec = if( nequal(stim_file "") parse_data_from_file(stim_file) nil )

        ; modify config view
        when( conf_list
            edit_config_view(tb_lib tb_cell "config" conf_list)
        )

        sev_sess_sess = open_adel_session(tb_lib tb_cell "config" tb_ade_view session_name "a")
        sev_session = car(sev_sess_sess)
        session = cadr(sev_sess_sess)

        ; change corners, parameters, and run options
        asiRemoveAllModelLibSelection(session)
        asiAddModelLibSelection(session corner_file car(corner_list))

        set_adel_parameters( session param_values )

        ; apply BAG stimuli (before the state save below, so it persists)
        bag_apply_stimuli(session tb_lib tb_cell stim_spec)

        ; delete original ade view to update
        state_obj = ddGetObj(tb_lib tb_cell tb_ade_view)
        ddDeleteObj(state_obj)

        ; save and close
        asiSaveState(session ?name tb_ade_view ?option 'cellview ?lib tb_lib ?cell tb_cell)
        sevQuit(sev_session)
    ;)
)

; (ADE-L) create the simulation netlist deck without running the simulation.
; Opens the same config-view ADE-L session adel_run_simulation uses, loads the
; saved setup state, and (re)creates the netlist ADE would simulate
; (<projectDir>/<cell>/spectre/config/netlist/).  The direct simulator
; interfaces (bag/interface/direct.py family) re-run that deck outside
; Virtuoso, so this is their netlist provisioning step.  The saved state is
; left untouched.  Deck completion is verified by the python caller watching
; the deck file against a pre-call snapshot; this procedure only reports
; whether the netlist call itself failed.
procedure( adel_create_netlist(tb_lib tb_cell @optional (tb_ade_view "spectre_state1"))
    let( (session_name sev_sess_sess sev_session ok)
        sprintf(session_name "bag_netlist_adel_%s" getCurrentTime())
        sev_sess_sess = open_adel_session(tb_lib tb_cell "config" tb_ade_view session_name "a")
        sev_session = car(sev_sess_sess)
        ; prefer the full-recreate call so config-view or state edits made
        ; since the last run are always picked up; fall back to the
        ; incremental netlist call on ADE builds that lack it.
        ok = if( isCallable('sevRecreateNetlist) then
            errset(sevRecreateNetlist(sev_session) t)
        else
            errset(sevNetlist(sev_session) t)
        );if
        errset(sevQuit(sev_session) t)
        unless( ok
            error("adel_create_netlist failed for %s__%s: %L" tb_lib tb_cell errset.errset)
        );unless
        t
    );let
);procedure

; Run state is kept only for BAG-owned ADE sessions. A poll never sleeps:
; returning to the event loop lets ADE finish and evaluate its outputs.
unless(boundp('bag_adel_runs) bag_adel_runs = makeTable('bag_adel_runs nil))

; ADE-L caches asimenv save/exit preferences in each session at creation.
; Changing envSetVal just before sevQuit does not change an existing session.
; Disable queries only on the BAG-owned session; global preferences stay intact.
procedure(bag_adel_quit(sev_session)
    unless(isCallable('sevSetSaveQuery) && isCallable('_sevSetADELExitQuery)
        error("ADE-L session query setters unavailable; refusing modal cleanup")
    )
    sevSetSaveQuery(sev_session nil)
    _sevSetADELExitQuery(sev_session nil)
    sevQuit(sev_session)
)

procedure(adel_start_simulation(tb_lib tb_cell token @optional (tb_ade_view "spectre_state1"))
    let((pair run p line count)
        when(bag_adel_runs[token] error("ADE-L run token already active: %s" token))
        pair = open_adel_session(tb_lib tb_cell "config" tb_ade_view token "a")
        run = makeTable('bag_adel_run nil)
        run['sev] = car(pair)
        run['asi] = cadr(pair)
        bag_adel_runs[token] = run
        run['log] = hiGetLogFileName()
        count = 0
        p = infile(run['log])
        when(p
            while(gets(line p) count = count + 1)
            close(p)
        )
        run['line] = count
        sevNetlistAndRun(run['sev])
        t
    )
)

procedure(adel_poll_simulation(token result_file)
    let((run p line count finish failure outputs)
        run = bag_adel_runs[token]
        unless(run error("Unknown ADE-L run token: %s" token))
        count = 0
        p = infile(run['log])
        when(p
            while(gets(line p)
                count = count + 1
                when(count > run['line]
                    cond(
                        (index(line "INFO (ADE-3071): Simulation completed successfully.")
                            finish = t)
                        (!failure && (index(line "ERROR (") || index(line "ERROR:") ||
                                      index(line "*Error*") || index(line "INFO (ADE-3069):"))
                            failure = line)
                    )
                )
            )
            close(p)
        )
        p = outfile(result_file "w")
        cond(
            (finish
                fprintf(p "status: complete\noutputs:\n")
                outputs = setof(out asiGetOutputList(run['asi]) out->name)
                if(outputs then
                    foreach(out outputs fprintf(p "  %L: %L\n" out->name out->value))
                else
                    fprintf(p "  {}\n")
                )
            )
            (failure
                ; A YAML literal preserves quotes/backslashes from tool errors.
                fprintf(p "status: error\ndetail: |\n  %s\n" failure))
            (t fprintf(p "status: pending\n"))
        )
        close(p)
        t
    )
)

procedure(adel_close_simulation(token)
    let((run)
        run = bag_adel_runs[token]
        when(run
            bag_adel_quit(run['sev])
            remove(token bag_adel_runs)
        )
        t
    )
)

; Fail fast for clients predating the asynchronous protocol. A blocking
; compatibility implementation would reintroduce the event-loop deadlock.
procedure(adel_run_simulation(@rest args)
    error("Reload BAG Python and use AdelSession.run_simulation (asynchronous ADE-L protocol)")
)
