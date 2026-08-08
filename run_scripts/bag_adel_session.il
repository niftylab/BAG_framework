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
procedure( adel_modify_testbench(tb_lib tb_cell conf_file opt_file corner_file sim_env_file param_file 
                                 env_params_file @optional (tb_ade_view "spectre_state1") "ttttttt")
    ;let( (sev_sess_sess sev_session session sdb conf_list run_params corner_list env_param_values session_name)
        sprintf(session_name "bag_sim_adel_%s" getCurrentTime())

        ; read inputs from file.
        conf_list = parse_data_from_file(conf_file)
        run_params = parse_data_from_file(opt_file)
        ;corner_file
        corner_list = parse_data_from_file(sim_env_file)
        param_values = parse_data_from_file(param_file)
        env_param_values = parse_data_from_file(env_params_file)

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

        ; delete original ade view to update
        state_obj = ddGetObj(tb_lib tb_cell tb_ade_view)
        ddDeleteObj(state_obj)

        ; save and close
        asiSaveState(session ?name tb_ade_view ?option 'cellview ?lib tb_lib ?cell tb_cell)
        sevQuit(sev_session)
    ;)
)

; run an ade-l simulation.
; Polls the CIW log until the ADE-L run finishes.  A failed netlist/run or a
; poll timeout closes the ADE session and raises error() instead of polling
; forever, so a failed run no longer blocks the BAG skill server until
; Virtuoso is restarted.  timeout bounds the total wait in seconds; after the
; first error line polling continues only err_grace more seconds in case the
; completion message still arrives (recoverable errors lose to a success).
procedure( adel_run_simulation(tb_lib tb_cell res_file_name @optional (tb_ade_view "spectre_state1") (timeout 3600) (err_grace 30))
    let( (session_name sev_sess_sess sev_session session p line start_line
          log_lines line_no finish fail_reason elapsed deadline out_list state_obj)
        sprintf(session_name "bag_sim_adel_%s" getCurrentTime())
        ; remember how many log lines exist before the run starts so only
        ; output from this run is scanned: a stale ADE-3071/error line from a
        ; previous run in the same session must not affect this run.
        start_line = 0
        p = infile( hiGetLogFileName())
        when( p
            while( gets( line p )
                start_line = start_line + 1
            ); while
            close(p)
        );when
        sev_sess_sess = open_adel_session(tb_lib tb_cell "config" tb_ade_view session_name "a")
        sev_session = car(sev_sess_sess)
        session = cadr(sev_sess_sess)
        ;asiNetlist(session)
        ;asiRunSimulation(session)
        sevNetlistAndRun(sev_session)
        ;wait until the simulation is completed, failed, or timed out.
        finish = nil
        fail_reason = nil
        elapsed = 0
        deadline = timeout
        while( !finish && elapsed < deadline
            sleep(5)  ; wait to allow the simulation to run.
            elapsed = elapsed + 5
            ; load the log lines appended after the run started.
            log_lines = list()
            line_no = 0
            p = infile( hiGetLogFileName())
            when( p
                while( gets( line p )
                    line_no = line_no + 1
                    when( line_no > start_line
                        log_lines = append1(log_lines line)
                    );when
                ); while
                close(p)
            );when
            foreach( log_line log_lines
                cond(
                    ( index(log_line "INFO (ADE-3071): Simulation completed successfully.")
                        printf( "*INFO* Simulation completed \n")
                        finish = 't
                    )
                    ( !fail_reason && (index(log_line "ERROR (") || index(log_line "*Error*") || substring(log_line 1 2) == "\\e")
                        fail_reason = log_line
                    )
                );cond
            );foreach
            ; after the first error line keep polling only a short grace
            ; period in case the completion message still arrives.
            when( fail_reason && deadline > elapsed + err_grace
                deadline = elapsed + err_grace
            );when
        ); while
        unless( finish
            errset(sevQuit(sev_session) t)
            unless( fail_reason
                sprintf(fail_reason "no completion message within %L seconds" timeout)
            );unless
            error("adel_run_simulation failed for %s__%s: %s" tb_lib tb_cell fail_reason)
        );unless
        ; read out the results.
        out_list=asiGetOutputList(session)
        p = outfile( res_file_name "w" )
        foreach( out out_list
            if((out~>name) then
                fprintf( p "%s: %L\n" out~>name out~>value )
            );if
        );foreach
        close(p)
        ; delete original ade view to update
        state_obj = ddGetObj(tb_lib tb_cell tb_ade_view)
        ddDeleteObj(state_obj)

        ; save and close
        asiSaveState(session ?name tb_ade_view ?option 'cellview ?lib tb_lib ?cell tb_cell)
        sevQuit(sev_session)
    );let
);procedure
