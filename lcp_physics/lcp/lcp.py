from enum import Enum

import torch
from torch.autograd import Function

from .solvers import batch_pdipm as pdipm_b
from .util import bger, expandParam, extract_nBatch


class LCPSolvers(Enum):
    PDIPM_BATCHED = 1


class LCPFunction(Function):
    def __init__(self, eps=1e-12, verbose=-1, notImprovedLim=3,
                 maxIter=10, solver=LCPSolvers.PDIPM_BATCHED):
        super().__init__()
        self.eps = eps
        self.verbose = verbose
        self.notImprovedLim = notImprovedLim
        self.maxIter = maxIter
        self.solver = solver
        self.Q_LU = self.S_LU = self.R = None

    def forward(self, Q_, p_, G_, h_, A_, b_, F_):
        # TODO Write detailed documentation.
        """Solve a batch of mixed LCPs.
        """

        nBatch = extract_nBatch(Q_, p_, G_, h_, A_, b_)
        Q, _ = expandParam(Q_, nBatch, 3)
        p, _ = expandParam(p_, nBatch, 2)
        G, _ = expandParam(G_, nBatch, 3)
        h, _ = expandParam(h_, nBatch, 2)
        A, _ = expandParam(A_, nBatch, 3)
        b, _ = expandParam(b_, nBatch, 2)
        F, _ = expandParam(F_, nBatch, 3)

        _, nineq, nz = G.size()
        neq = A.size(1) if A.ndimension() > 0 else 0
        assert(neq > 0 or nineq > 0)
        self.neq, self.nineq, self.nz = neq, nineq, nz

        if self.solver == LCPSolvers.PDIPM_BATCHED:
            self.Q_LU, self.S_LU, self.R = pdipm_b.pre_factor_kkt(Q, G, F, A)
            zhats, self.nus, self.lams, self.slacks = pdipm_b.forward(
                Q, p, G, h, A, b, F, self.Q_LU, self.S_LU, self.R,
                self.eps, self.verbose, self.notImprovedLim,
                self.maxIter, solver=pdipm_b.KKTSolvers.LU_PARTIAL)
        else:
            assert False

        # self.verify_lcp(zhats, Q, G, A, F, p, h)
        self.save_for_backward(zhats, Q_, p_, G_, h_, A_, b_, F_)
        return zhats

    def backward(self, dl_dzhat):
        zhats, Q, p, G, h, A, b, F = self.saved_tensors
        nBatch = extract_nBatch(Q, p, G, h, A, b)
        Q, Q_e = expandParam(Q, nBatch, 3)
        p, p_e = expandParam(p, nBatch, 2)
        G, G_e = expandParam(G, nBatch, 3)
        h, h_e = expandParam(h, nBatch, 2)
        A, A_e = expandParam(A, nBatch, 3)
        b, b_e = expandParam(b, nBatch, 2)
        F, F_e = expandParam(F, nBatch, 3)

        neq, nineq, nz = self.neq, self.nineq, self.nz

        # D = torch.diag((self.lams / self.slacks).squeeze(0)).unsqueeze(0)
        d = self.lams / self.slacks

        pdipm_b.factor_kkt(self.S_LU, self.R, d)
        dx, _, dlam, dnu = pdipm_b.solve_kkt(self.Q_LU, d, G, A, self.S_LU,
            dl_dzhat, torch.zeros(nBatch, nineq).type_as(G),
            torch.zeros(nBatch, nineq).type_as(G),
            torch.zeros(nBatch, neq).type_as(G))

        dps = dx
        dGs = (bger(dlam, zhats) + bger(self.lams, dx))
        if G_e:
            dGs = dGs.mean(0).squeeze(0)
        dFs = (bger(dlam, self.lams) + bger(self.lams, dlam))
        # dFs = torch.ones(dFs.size()).double()
        if F_e:
            assert False  # TODO
        dhs = -dlam
        if h_e:
            dhs = dhs.mean(0).squeeze(0)
        if neq > 0:
            dAs = bger(dnu, zhats) + bger(self.nus, dx)
            dbs = -dnu
            if A_e:
                dAs = dAs.mean(0).squeeze(0)
            if b_e:
                dbs = dbs.mean(0).squeeze(0)
        else:
            dAs, dbs = None, None
        dQs = 0.5 * (bger(dx, zhats) + bger(zhats, dx))
        if Q_e:
            dQs = dQs.mean(0).squeeze(0)

        grads = (dQs, dps, dGs, dhs, dAs, dbs, dFs)
        return grads

    def verify_lcp(self, zhats, Q, G, A, F, p, h):
        epsilon = 1e-7

        c1 = (self.slacks >= 0).all()
        c2 = (self.lams >= 0).all()
        c3 = (torch.abs(self.slacks * self.lams) < epsilon).all()
        conds = c1 and c2 and c3
        l1 = Q.matmul(zhats.unsqueeze(2)) + G.transpose(1, 2).matmul(self.lams.unsqueeze(2)) \
             + p.unsqueeze(2)
        if A.dim() > 0:
            l1 += A.transpose(1, 2).matmul(self.nus.unsqueeze(2))
        # XXX Flipped signs for G*z. Why?
        l2 = -G.matmul(zhats.unsqueeze(2)) + F.matmul(self.lams.unsqueeze(2)) \
             + h.unsqueeze(2) - self.slacks.unsqueeze(2)
        l3 = A.matmul(zhats.unsqueeze(2)) if A.dim() > 0 else torch.Tensor([0])
        lcp = (torch.abs(l1) < epsilon).all() and (torch.abs(l2) < epsilon).all() \
              and (torch.abs(l3) < epsilon).all()

        if not conds:
            print('Complementarity conditions have imprecise solution.')
        if not lcp:
            print('LCP has imprecise solution.')
        return conds and lcp
