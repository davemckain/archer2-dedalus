"""
Dedalus script for 2D Rayleigh-Benard convection.

This script uses a Fourier basis in the x direction with periodic boundary
conditions.  The equations are scaled in units of the buoyancy time (Fr = 1).
The hydro equations utilize a vorticity formulation for the diffusive terms.

This version of the script is intended for scaling and performance tests.

Usage:
    rayleigh_benard_2d.py [options]

Options:
    --nz=<nz>                 Number of Chebyshev modes [default: 128]
    --nx=<nx>                 Number of Fourier modes; default is aspect*nz
    --aspect=<aspect>         Aspect ratio [default: 2]
    --Rayleigh=<Rayleigh>     Rayleigh number of the convection [default: 1e6]

    --niter=<niter>           Iterations to run scaling test for (+1 automatically added to account for startup) [default: 100]
"""

import numpy as np
from mpi4py import MPI
import time

from dedalus import public as de
from dedalus.extras import flow_tools

import logging
logger = logging.getLogger(__name__)

initial_time = time.time()

from docopt import docopt
args = docopt(__doc__)
nz = int(args['--nz'])
nx = args['--nx']
aspect = int(args['--aspect'])
if nx is None:
    nx = nz*aspect
else:
    nx = int(nx)

Rayleigh_string = args['--Rayleigh']
Rayleigh = float(Rayleigh_string)

# 2-D problem; no processor mesh to define
mesh = None

# Parameters
Lx, Lz = (aspect, 1.)
Prandtl = 1.
MagneticPrandtl = 1.

# Create bases and domain
x_basis = de.Fourier(  'x', nx, interval=(0, Lx), dealias=3/2)
z_basis = de.Chebyshev('z', nz, interval=(0, Lz), dealias=3/2)
domain = de.Domain([x_basis, z_basis], grid_dtype=np.float64, mesh=mesh)

# 3D Boussinesq magnetohydrodynamics with vector potential formulism
problem = de.IVP(domain, variables=['T','T_z','Ox','Oy','p','u','w'])
problem.meta['p','T','u','w']['z']['dirichlet'] = True

problem.substitutions['v'] = '0'
problem.substitutions['dy(A)'] = '0*A'
problem.substitutions['UdotGrad(A,A_z)'] = '(u*dx(A) + v*dy(A) + w*(A_z))'
problem.substitutions['Lap(A,A_z)'] = '(dx(dx(A)) + dy(dy(A)) + dz(A_z))'
problem.substitutions['Oz'] = '(dx(v)  - dy(u))'
problem.substitutions['Kx'] = '(dy(Oz) - dz(Oy))'
problem.substitutions['Ky'] = '(dz(Ox) - dx(Oz))'
problem.substitutions['Kz'] = '(dx(Oy) - dy(Ox))'

problem.parameters['P'] = (Rayleigh * Prandtl)**(-1/2)
problem.parameters['R'] = (Rayleigh / Prandtl)**(-1/2)
problem.parameters['F'] = F = 1
problem.parameters['pi'] = np.pi
problem.add_equation("dt(T) - P*Lap(T, T_z)         - F*w = -UdotGrad(T, T_z)")
# O == omega = curl(u);  K = curl(O)
problem.add_equation("dt(u)  + R*Kx  + dx(p)              =  v*Oz - w*Oy")
problem.add_equation("dt(w)  + R*Kz  + dz(p)    -T        =  u*Oy - v*Ox")
problem.add_equation("dx(u) + dy(v) + dz(w) = 0")
problem.add_equation("Ox + dz(v) - dy(w) = 0")
problem.add_equation("Oy - dz(u) + dx(w) = 0")
problem.add_equation("T_z - dz(T) = 0")
problem.add_bc("left(T) = 0")
problem.add_bc("left(u) = 0")
problem.add_bc("left(w) = 0")
problem.add_bc("right(T) = 0")
problem.add_bc("right(u) = 0")
problem.add_bc("right(w) = 0", condition="(nx != 0)")
problem.add_bc("right(p) = 0", condition="(nx == 0)")

# Build solver
#solver = problem.build_solver(de.timesteppers.RK443)
solver = problem.build_solver(de.timesteppers.RK222)
logger.info('Solver built')

# Initial conditions
x = domain.grid(0)
z = domain.grid(-1)
T = solver.state['T']

# Random perturbations, initialized globally for same results in parallel
gshape = domain.dist.grid_layout.global_shape(scales=1)
slices = domain.dist.grid_layout.slices(scales=1)
rand = np.random.RandomState(seed=42)
noise = rand.standard_normal(gshape)[slices]

# Linear background + perturbations damped at walls
zb, zt = z_basis.interval
pert =  1e-3 * noise * (zt - z) * (z - zb)
T['g'] = F * pert
# poor (or rich?) man's coeff filter.
# if you set to scales(1), see obvious divU error early on; at 1/2 or 1/4, no divU error
T.set_scales(1/4, keep_data=True)
T['c']
T['g']
T.set_scales(1, keep_data=True)

# Initial timestep
dt = 1e-3 #0.125

niter = int(float(args['--niter']))+1
# Integration parameters
solver.stop_sim_time = 50
solver.stop_wall_time = 30 * 60.
solver.stop_iteration = niter

max_dt = 0.5
# CFL
CFL = flow_tools.CFL(solver, initial_dt=dt, cadence=1, safety=0.8/2,
                     max_change=1.5, min_change=0.5, max_dt=max_dt, threshold=0.05)
CFL.add_velocities(('u', 'w'))

# Main
try:
    logger.info('Starting loop')
    first_loop = True
    while solver.ok:
        dt = CFL.compute_dt()
        dt = solver.step(dt)
        log_string = 'Iteration: %i, Time: %e, dt: %e' %(solver.iteration, solver.sim_time, dt)
        logger.info(log_string)
        if first_loop:
            start_time = time.time()
            first_loop = False
except:
    logger.error('Exception raised, triggering end of main loop.')
    raise
finally:
    end_time = time.time()
    logger.info('Iterations: %i' %solver.iteration)
    logger.info('Sim end time: %f' %solver.sim_time)
    logger.info('Run time: %.2f sec' %(end_time-start_time))
    logger.info('Run time: %f cpu-hr' %((end_time-start_time)/60/60*domain.dist.comm_cart.size))

    if (domain.distributor.rank==0):
        N_TOTAL_CPU = domain.distributor.comm_cart.size

        # Print statistics
        print('-' * 40)
        total_time = end_time-initial_time
        main_loop_time = end_time - start_time
        startup_time = start_time-initial_time
        n_steps = solver.iteration-1
        print('  startup time:', startup_time)
        print('main loop time:', main_loop_time)
        print('    total time:', total_time)
        print('    iterations:', solver.iteration)
        print(' loop sec/iter:', main_loop_time/solver.iteration)
        print('    average dt:', solver.sim_time / n_steps)
        print("          N_cores, Nx, Nz, startup     main loop,   main loop/iter, main loop/iter/grid, n_cores*main loop/iter/grid")
        print('scaling:',
              ' {:d} {:d} {:d}'.format(N_TOTAL_CPU,nx,nz),
              ' {:8.3g} {:8.3g} {:8.3g} {:8.3g} {:8.3g}'.format(startup_time,
                                                                main_loop_time,
                                                                main_loop_time/n_steps,
                                                                main_loop_time/n_steps/(nx*nz),
                                                                N_TOTAL_CPU*main_loop_time/n_steps/(nx*nz)))
        print('-' * 40)
