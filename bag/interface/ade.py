# -*- coding: utf-8 -*-

"""ADE session flavors for the skill database interface.

Virtuoso has several simulation front-ends (ADE-XL, ADE-L, Maestro), each
driven through its own SKILL function family
(``run_scripts/bag_adexl_session.il`` / ``bag_adel_session.il``).  Each
flavor lives in its own session class here;
:class:`~bag.interface.skill.SkillInterface` delegates every testbench
operation to the session selected by the ``testbench.flavor`` entry of the
database configuration (falling back to the interface class default).
"""

from typing import List, Dict, Optional, Tuple

import os

import yaml

import bag
from .skill import to_skill_list_str


class AdeSession(object):
    """Base class for one ADE flavor's testbench operations.

    Parameters
    ----------
    db : :class:`bag.interface.skill.SkillInterface`
        the database interface used to evaluate skill expressions.
    """

    #: flavor name used in ``db_config['testbench']['flavor']``.
    flavor = ''

    def __init__(self, db):
        self.db = db

    @property
    def db_config(self):
        return self.db.db_config

    @property
    def tmp_dir(self):
        return self.db.tmp_dir

    def _eval_skill(self, expr, input_files=None, out_file=None):
        return self.db._eval_skill(expr, input_files=input_files, out_file=out_file)

    def configure_testbench(self, tb_lib, tb_cell):
        raise NotImplementedError('%s does not implement configure_testbench.'
                                  % type(self).__name__)

    def get_testbench_info(self, tb_lib, tb_cell):
        raise NotImplementedError('%s does not implement get_testbench_info.'
                                  % type(self).__name__)

    def update_testbench(self, lib, cell, parameters, sim_envs, config_rules,
                         env_parameters):
        raise NotImplementedError('%s does not implement update_testbench.'
                                  % type(self).__name__)

    def run_simulation(self, lib, cell, res_file_name=None):
        raise NotImplementedError(
            '%s does not run simulations through the database interface; '
            'use the simulation interface instead.' % type(self).__name__)


class AdexlSession(AdeSession):
    """ADE-XL session flavor (``bag_adexl_session.il`` function family).

    Simulations themselves run through the simulation interface
    (e.g. :class:`bag.interface.ocean.OceanInterface`), not this class.
    """

    flavor = 'adexl'

    def configure_testbench(self, tb_lib, tb_cell):
        """Update testbench state for the given testbench.

        This method fill in process-specific information for the given testbench.

        Parameters
        ----------
        tb_lib : str
            testbench library name.
        tb_cell : str
            testbench cell name.

        Returns
        -------
        cur_env : str
            the current simulation environment.
        envs : list[str]
            a list of available simulation environments.
        parameters : dict[str, str]
            a list of testbench parameter values, represented as string.
        """
        tb_config = self.db_config['testbench']

        cmd = ('instantiate_testbench("{tb_cell}" "{targ_lib}" ' +
               '"{config_libs}" "{config_views}" "{config_stops}" ' +
               '"{default_corner}" "{corner_file}" {def_files} ' +
               '"{tech_lib}" {result_file})')
        cmd = cmd.format(tb_cell=tb_cell,
                         targ_lib=tb_lib,
                         config_libs=tb_config['config_libs'],
                         config_views=tb_config['config_views'],
                         config_stops=tb_config['config_stops'],
                         default_corner=tb_config['default_env'],
                         corner_file=tb_config['env_file'],
                         def_files=to_skill_list_str(tb_config['def_files']),
                         tech_lib=self.db_config['schematic']['tech_lib'],
                         result_file='{result_file}')
        output = yaml.load(self._eval_skill(cmd, out_file='result_file'), Loader=yaml.FullLoader)
        return tb_config['default_env'], output['corners'], output['parameters'], output['outputs']

    def get_testbench_info(self, tb_lib, tb_cell):
        """Returns information about an existing testbench.

        Parameters
        ----------
        tb_lib : str
            testbench library.
        tb_cell : str
            testbench cell.

        Returns
        -------
        cur_envs : list[str]
            the current simulation environments.
        envs : list[str]
            a list of available simulation environments.
        parameters : dict[str, str]
            a list of testbench parameter values, represented as string.
        outputs : dict[str, str]
            a list of testbench output expressions.
        """
        cmd = 'get_testbench_info("{tb_lib}" "{tb_cell}" {result_file})'
        cmd = cmd.format(tb_lib=tb_lib,
                         tb_cell=tb_cell,
                         result_file='{result_file}')
        output = yaml.load(self._eval_skill(cmd, out_file='result_file'), Loader=yaml.FullLoader)
        return output['enabled_corners'], output['corners'], output['parameters'], output['outputs']

    def update_testbench(self,
                         lib,  # type: str
                         cell,  # type: str
                         parameters,  # type: Dict[str, str]
                         sim_envs,  # type: List[str]
                         config_rules,  # type: List[List[str]]
                         env_parameters  # type: List[List[Tuple[str, str]]]
                         ):
        # type: (...) -> None
        """Update the given testbench configuration.

        Parameters
        ----------
        lib : str
            testbench library.
        cell : str
            testbench cell.
        parameters : Dict[str, str]
            testbench parameters.
        sim_envs : List[str]
            list of enabled simulation environments.
        config_rules : List[List[str]]
            config view mapping rules, list of (lib, cell, view) rules.
        env_parameters : List[List[Tuple[str, str]]]
            list of param/value list for each simulation environment.
        """

        cmd = ('modify_testbench("%s" "%s" {conf_rules} {run_opts} '
               '{sim_envs} {params} {env_params})' % (lib, cell))
        in_files = {'conf_rules': config_rules,
                    'run_opts': [],
                    'sim_envs': sim_envs,
                    'params': list(parameters.items()),
                    'env_params': list(zip(sim_envs, env_parameters)),
                    }
        self._eval_skill(cmd, input_files=in_files)


class AdelSession(AdexlSession):
    """ADE-L session flavor (``bag_adel_session.il`` function family).

    The testbench is opened in place: the OA schematic, config view, and
    the saved ADE state (``spectre_state1``) must already exist in the
    testbench library.  ``run_simulation`` drives ``adel_run_simulation``
    through the skill server.

    Inherits :class:`AdexlSession` so ``get_testbench_info`` keeps its
    historical (ADE-XL path) behavior, matching the old
    ``ADELSkillInterface(SkillInterface)`` override relationship.
    """

    flavor = 'adel'

    lib_name = None   # library name of the last configured testbench
    cell_name = None  # cell name of the last configured testbench

    def configure_testbench(self, tb_lib, tb_cell):
        """Update testbench state for the given testbench.

        This method fill in process-specific information for the given testbench.

        Parameters
        ----------
        tb_lib : str
            testbench library name.
        tb_cell : str
            testbench cell name.

        Returns
        -------
        cur_env : str
            the current simulation environment.
        envs : list[str]
            a list of available simulation environments.
        parameters : dict[str, str]
            a list of testbench parameter values, represented as string.
        """
        self.lib_name = tb_lib
        self.cell_name = tb_cell
        # mirrored on the interface for backward compatibility.
        self.db.lib_name = tb_lib
        self.db.cell_name = tb_cell

        tb_config = self.db_config['testbench']

        cmd = ('adel_instantiate_testbench("{tb_cell}" "{targ_lib}" ' +
               '"{config_libs}" "{config_views}" "{config_stops}" ' +
               '"{default_corner}" "{corner_file}" {def_files} ' +
               '"{tech_lib}" {result_file})')
        cmd = cmd.format(tb_cell=tb_cell,
                         targ_lib=tb_lib,
                         config_libs=tb_config['config_libs'],
                         config_views=tb_config['config_views'],
                         config_stops=tb_config['config_stops'],
                         default_corner=tb_config['default_env'],
                         corner_file=tb_config['env_file'],
                         def_files=to_skill_list_str(tb_config['def_files']),
                         tech_lib=self.db_config['schematic']['tech_lib'],
                         result_file='{result_file}')
        output = yaml.load(self._eval_skill(cmd, out_file='result_file'), Loader=yaml.FullLoader)
        return tb_config['default_env'], output['corners'], output['parameters'], output['outputs']

    def update_testbench(self,
                         lib,  # type: str
                         cell,  # type: str
                         parameters,  # type: Dict[str, str]
                         sim_envs,  # type: List[str]
                         config_rules,  # type: List[List[str]]
                         env_parameters  # type: List[List[Tuple[str, str]]]
                         ):
        # type: (...) -> None
        """Update the given testbench configuration.

        Parameters
        ----------
        lib : str
            testbench library.
        cell : str
            testbench cell.
        parameters : Dict[str, str]
            testbench parameters.
        sim_envs : List[str]
            list of enabled simulation environments.
        config_rules : List[List[str]]
            config view mapping rules, list of (lib, cell, view) rules.
        env_parameters : List[List[Tuple[str, str]]]
            list of param/value list for each simulation environment.
        """

        tb_config = self.db_config['testbench']
        corner_file = tb_config['env_file']
        cmd = ('adel_modify_testbench("%s" "%s" {conf_rules} {run_opts} "%s" '
               '{sim_envs} {params} {env_params})' % (lib, cell, corner_file))
        in_files = {'conf_rules': config_rules,
                    'run_opts': [],
                    'sim_envs': sim_envs,
                    'params': list(parameters.items()),
                    'env_params': list(zip(sim_envs, env_parameters)),
                    }
        self._eval_skill(cmd, input_files=in_files)

    def run_simulation(self, lib, cell, res_file_name=None):
        """Run ADE-L simulation"""
        if res_file_name is None:
            res_file_name = 'sim_results.yaml'
        save_dir = bag.io.make_temp_dir(prefix='adel_data', parent_dir=self.tmp_dir)
        save_full_path = save_dir + '/' + res_file_name
        cmd = ('adel_run_simulation("%s" "%s" "%s")' % (lib, cell, save_full_path))
        self._eval_skill(cmd)
        if os.path.exists(save_full_path):
            with open(save_full_path, 'r') as stream:
                results = yaml.load(stream, Loader=yaml.FullLoader)
        return results


class MaestroSession(AdeSession):
    """Maestro (ADE Assembler) session flavor -- not implemented yet.

    tbadc-style testbenches store their setup in ``maestro`` views; they
    will be driven here once the ``bag_maestro_session.il`` SKILL family is
    implemented.  Until then convert the testbench to an ADE-L
    ``spectre_state1`` state, or use an adexl/adel testbench.
    """

    flavor = 'maestro'

    def _not_implemented(self):
        raise NotImplementedError(
            'Maestro ADE sessions are not implemented yet; convert the '
            'testbench to an ADE-L spectre_state1 state or use the adexl/'
            'adel flavor.')

    def configure_testbench(self, tb_lib, tb_cell):
        self._not_implemented()

    def get_testbench_info(self, tb_lib, tb_cell):
        self._not_implemented()

    def update_testbench(self, lib, cell, parameters, sim_envs, config_rules,
                         env_parameters):
        self._not_implemented()

    def run_simulation(self, lib, cell, res_file_name=None):
        self._not_implemented()


SESSION_CLASSES = {cls.flavor: cls
                   for cls in (AdexlSession, AdelSession, MaestroSession)}


def create_ade_session(flavor, db):
    """Create the session object for the given ADE flavor name.

    Parameters
    ----------
    flavor : str
        the ADE flavor name ('adexl', 'adel', or 'maestro').
    db : :class:`bag.interface.skill.SkillInterface`
        the database interface the session drives skill commands through.

    Returns
    -------
    session : :class:`AdeSession`
        the session object.
    """
    try:
        cls = SESSION_CLASSES[flavor]
    except KeyError:
        raise ValueError('Unknown ADE session flavor: %r (choices: %s)'
                         % (flavor, ', '.join(sorted(SESSION_CLASSES))))
    return cls(db)
