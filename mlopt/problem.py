from multiprocessing import Pool, cpu_count
from itertools import repeat
import numpy as np
#  from .solvers.solvers import SOLVER_MAP, DEFAULT_SOLVER
#  from .solvers.statuses import SOLUTION_PRESENT
from .strategy import Strategy
from .settings import TOL, DEFAULT_SOLVER
# Import cvxpy and constraint types
import cvxpy as cp
from cvxpy.constraints.nonpos import NonPos
from cvxpy.constraints.zero import Zero
from tqdm import tqdm


class OptimizationProblem(object):

    def __init__(self, cvxpy_problem, name="problem"):
        """Initialize Optimization problem

        Parameters
        ----------
        cvxpy_problem: cvxpy Problem
            Problem generated in CVXPY.
        name : string
            Problem name for outputting learner.
        """
        self.name = name
        self.cvxpy_problem = cvxpy_problem

    def populate(self, theta):
        """
        Populate problem using parameter theta
        """
        for p in self.cvxpy_problem.parameters():
            p.value = theta[p.name()]

    @property
    def num_var(self):
        """Number of variables"""
        return sum([x.size for x in self.cvxpy_problem.variables()])

    @property
    def num_constraints(self):
        """Number of variables"""
        return sum([x.size for x in self.cvxpy_problem.constraints])

    def cost(self):
        """Compute cost function value"""
        return self.cvxpy_problem.objective.value

    def is_mip(self):
        """Check if problem has integer variables."""
        return self.cvxpy_problem.is_mixed_integer()

    #  def infeasibility(self, variables):
    def infeasibility(self):
        """
        Compute infeasibility for variables
        """
        # Assign variables
        #  for x in variables:
        #      for v in self.cvxpy_problem.variables():
        #          if v.id == x.id:
        #              v.value = x.value

        # Compute violations
        violations = np.concatenate([np.atleast_1d(c.violation())
                                     for c in self.cvxpy_problem.constraints])

        return np.linalg.norm(violations)

    def _solve(self, problem, solver, settings):
        """
        Solve problem with CVXPY and return results dictionary
        """
        problem.solve(solver=solver,
                      #  verbose=True,
                      **settings)

        results = {}
        results['time'] = problem.solver_stats.solve_time
        results['x'] = np.concatenate([np.atleast_1d(v.value)
                                       for v in problem.variables()])
        results['cost'] = np.inf

        if problem.status in cp.settings.SOLUTION_PRESENT:
            results['cost'] = self.cost()
            results['infeasibility'] = self.infeasibility()

            if not problem.is_mixed_integer():
                binding_constraints = dict()
                for c in problem.constraints:
                    binding_constraints[c.id] = \
                        np.array([1 if abs(y) >= TOL else 0
                                  for y in np.atleast_1d(c.dual_value)])
                results['binding_constraints'] = binding_constraints

                # DEBUG
                #  n_binding = sum([sum(x) for x in binding_constraints.values()])
                #  n_var = sum([x.size for x in problem.variables()])
                #  if n_binding < n_var:
                #      print("Number of binding constraints: ", n_binding)
                #      print("Number of variables: ", n_var)
                #      #  import ipdb; ipdb.set_trace()
        else:
            results['cost'] = np.inf
            results['infeasibility'] = np.inf
            results['binding_constraints'] = dict()

        return results

    def _is_var_mip(self, var):
        """
        Is the cvxpy variable mixed-integer?
        """
        return var.attributes['boolean'] or var.attributes['integer']

    def _set_bool_var(self, var):
        """Set variable to be boolean"""
        var.attributes['boolean'] = True
        var.boolean_idx = list(np.ndindex(max(var.shape, (1,))))

    def _set_cont_var(self, var):
        """Set variable to be continuous"""
        var.attributes['boolean'] = False
        var.attributes['integer'] = False
        var.boolean_idx = []
        var.integer_idx = []

    def solve(self, solver=DEFAULT_SOLVER, **settings):
        """
        Solve optimization problem

        Parameters
        ----------
        solver : string, optional
            Solver to be used. Default Mosek.
        settings : dict, optional
            Solver settings. Default empty.

        Returns
        -------
        dict
            Results dictionary.
        """
        # Solve complete problem with CVXPY
        results = self._solve(self.cvxpy_problem, solver, settings)

        # Get solution and integer variables
        x_int = dict()

        if self.is_mip():
            # Get integer variables
            int_vars = [v for v in self.cvxpy_problem.variables()
                        if self._is_var_mip(v)]

            # Get value of integer variables
            x_int = dict()
            for x in int_vars:
                x_int[x.id] = np.array(x.value)

            # Change attributes to continuous
            int_vars_fix = []
            for x in int_vars:
                self._set_cont_var(x)  # Set continuous variable
                int_vars_fix += [x == x_int[x.id]]

            # Define new constraints to fix integer variables
            #  prob_fix_vars = cp.Problem(cp.Minimize(0), int_vars_fix)
            #  prob_cont = self.cvxpy_problem + prob_fix_vars
            prob_cont = cp.Problem(self.cvxpy_problem.objective,
                                   self.cvxpy_problem.constraints +
                                   int_vars_fix)

            # Solve
            results_cont = self._solve(prob_cont, solver, settings)

            # Get binding constraints from original problem
            binding_constraints = dict()
            for c in self.cvxpy_problem.constraints:
                binding_constraints[c.id] = \
                    results_cont['binding_constraints'][c.id]

            # Restore integer variables
            for x in int_vars:
                self._set_bool_var(x)
        else:
            binding_constraints = results['binding_constraints']

        # Get strategy
        strategy = Strategy(binding_constraints, x_int)

        # Define return dictionary
        return_dict = {}
        return_dict['x'] = results['x']
        return_dict['time'] = results['time']
        return_dict['cost'] = results['cost']
        return_dict['infeasibility'] = results['infeasibility']
        return_dict['strategy'] = strategy

        return return_dict

    def _verify_strategy(self, strategy):
        """Verify that strategy is compatible with current problem."""
        # Compare keys for binding constraints and integer variables
        variables = {v.id: v
                     for v in self.cvxpy_problem.variables()}
        con_keys = [c.id for c in self.cvxpy_problem.constraints]

        for key in strategy.binding_constraints.keys():
            if key not in con_keys:
                raise ValueError("Binding constraints not compatible " +
                                 "with problem. Constaint IDs not matching.")

        int_var_error = ValueError("Integer variables not compatible " +
                                   "with problem. Constaint IDs not " +
                                   "matching an integer variable.")
        for key in strategy.int_vars.keys():
            try:
                v = variables[key]
            except KeyError:
                raise int_var_error
            if not self._is_var_mip(v):
                raise int_var_error

    def solve_with_strategy(self, strategy, solver=DEFAULT_SOLVER,
                            **settings):
        """
        Solve problem using strategy

        Parameters
        ----------
        strategy : Strategy
            Strategy to be used.
        solver : string, optional
            Solver to be used. Default Mosek.
        settings : dict, optional
            Solver settings. Default empty.

        Returns
        -------
        dict
            Results.
        """
        self._verify_strategy(strategy)

        # Unpack strategy
        binding_constraints = strategy.binding_constraints
        int_vars = strategy.int_vars

        # Unpack original problem
        orig_objective = self.cvxpy_problem.objective
        orig_variables = self.cvxpy_problem.variables()
        orig_constraints = self.cvxpy_problem.constraints

        # Get same objective
        objective = orig_objective

        # Get only constraints in strategy
        constraints = []
        for con in orig_constraints:
            idx_binding = np.where(binding_constraints[con.id])[0]
            if len(idx_binding) > 0:
                # Binding constraints in expression
                con_expr = con.args[0]
                if con_expr.shape == ():
                    # Scalar case no slicing
                    binding_expr = con_expr
                else:
                    # Get binding constraints
                    binding_expr = con.args[0][idx_binding]

                # Set linear inequalities as equalities
                new_type = type(con)
                if type(con) == NonPos:
                    new_type = Zero

                constraints += [new_type(binding_expr)]

        # Fix integer variables
        int_fix = []
        for v in orig_variables:
            if self._is_var_mip(v):
                self._set_cont_var(v)
                int_fix += [v == int_vars[v.id]]

        # Solve problem
        prob_red = cp.Problem(objective, constraints + int_fix)
        results = self._solve(prob_red, solver, settings)
        #  results = self._solve(prob_red, solver, {'verbose': True})

        # Assign original problem variables to prob_red variables
        # TODO: Needed?
        #  prob_red_values = {v.id:v.value
        #                     for v in prob_red.variables()}
        #  for v in orig_variables:
        #      v.value = prob_red_values[v.id]

        # Make variables discrete again
        for v in self.cvxpy_problem.variables():
            if v.id in int_vars.keys():
                self._set_bool_var(v)

        return results

    def populate_and_solve(self, args):
        """Single function to populate the problem with
           theta and solve it with the solver. Useful for
           multiprocessing."""
        theta, solver, settings = args
        self.populate(theta)
        results = self.solve(solver, **settings)

        # DEBUG. Solve with other solvers and check
        #  res_gurobi = self.solve('GUROBI', settings)
        #  if res_gurobi[2] != results[2]:
        #      print("Wrong strategy")
        #      import ipdb; ipdb.set_trace()

        return results

    def solve_parametric(self, theta,
                         solver=DEFAULT_SOLVER, settings={},
                         message="Solving for all theta",
                         parallel=True  # Solve problems in parallel
                         ):
        """
        Solve parametric problems for each value of theta.

        Parameters
        ----------
        theta : DataFrame
            Parameter values.
        problem : Optimizationproblem
            Optimization problem to solve.
        solver : string, optional
            Solver to be used. Default Mosek.
        settings : dict, optional
            Solver settings. Default empty.
        parallel : bool, optional
            Solve problems in parallel. Default True.

        Returns
        -------
        dict
            Results dictionary.
        """
        n = len(theta)  # Number of points

        if parallel:
            print("Solving for all theta (parallel %i processors)..." %
                  cpu_count())
            #  self.pbar = tqdm(total=n, desc=message + " (parallel)")
            #  with tqdm(total=n, desc=message + " (parallel)") as self.pbar:
            with Pool(processes=min(n, cpu_count())) as pool:
                # Solve in parallel
                results = \
                    pool.map(self.populate_and_solve,
                             zip([theta.iloc[i, :] for i in range(n)],
                                 repeat(solver),
                                 repeat(settings)))
                # Solve in parallel
                #  results = pool.starmap(self._populate_and_solve,)
                # Solve in parallel and print tqdm progress bar
                #  results = list(tqdm(pool.imap(self._populate_and_solve,
                #                                zip([theta.iloc[i, :]
                #                                     for i in range(n)],
                #                                    repeat(solver),
                #                                    repeat(settings))),
                #                      total=n))
        else:
            # Preallocate solutions
            results = []
            for i in tqdm(range(n), desc=message + " (serial)"):
                results.append(self.populate_and_solve((theta.iloc[i, :],
                                                        solver, settings)))

        return results