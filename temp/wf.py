MPI_RANKS = 4
import numpy as np, adios2
from mpi4py import MPI
world = MPI.COMM_WORLD; rank, size = world.Get_rank(), world.Get_size()
role = 0 if rank < 2 else 1
comm = world.Split(color=role, key=rank)
lrank, lsize = comm.Get_rank(), comm.Get_size()
def simulate(comm):
    ad = adios2.Adios(comm); io = ad.declare_io("sim"); io.set_engine("SST")
    u = np.zeros(4, dtype=np.float64)
    var = io.define_variable("U", u, [lsize*4], [lrank*4], [4])
    e = io.open("st", adios2.bindings.Mode.Write, comm)
    for s in range(3):
        u[:] = float(s*10+lrank); e.begin_step(); e.put(var,u); e.end_step()
    e.close(); return f"producer{lrank}: 3 steps"
def analyze(comm):
    ad = adios2.Adios(comm); io = ad.declare_io("ana"); io.set_engine("SST")
    e = io.open("st", adios2.bindings.Mode.Read, comm); sums=[]
    while True:
        if e.begin_step() != adios2.bindings.StepStatus.OK: break
        v = io.inquire_variable("U"); n = int(v.shape()[0])
        out = np.zeros(n, dtype=np.float64); v.set_selection([[0],[n]])
        e.get(v,out); e.end_step(); sums.append(float(out.sum()))
    e.close(); return f"consumer{lrank}: {sums}"
msg = simulate(comm) if role==0 else analyze(comm)
for i in range(size):
    world.Barrier()
    if i==rank: print(msg, flush=True)
