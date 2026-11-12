"""検査所有の深い入力をdir_fdで作る。OSの絶対パス長へ依存しない。"""
import os

class DeepTree:
    def __init__(self,root,depth):
        self.root=root;self.depth=depth
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
        try:
            for _ in range(depth):
                os.mkdir('d',dir_fd=fd);child=os.open('d',os.O_RDONLY|os.O_DIRECTORY,dir_fd=fd);os.close(fd);fd=child
        finally:os.close(fd)
    def open(self,depth):
        fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
        try:
            for _ in range(depth):
                child=os.open('d',os.O_RDONLY|os.O_DIRECTORY,dir_fd=fd);os.close(fd);fd=child
            return fd
        except BaseException:os.close(fd);raise
    def detach(self,depth,destination):
        fd=self.open(depth)
        try:os.rename('d',destination,src_dir_fd=fd)
        finally:os.close(fd)
    def restore(self,depth,source):
        fd=self.open(depth)
        try:os.rename(source,'d',dst_dir_fd=fd)
        finally:os.close(fd)
    def leaf(self,depth,value):
        fd=self.open(depth)
        try:
            if value is None:os.unlink('leaf',dir_fd=fd)
            else:
                leaf=os.open('leaf',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=fd)
                try:os.write(leaf,value)
                finally:os.close(leaf)
        finally:os.close(fd)
    def identity(self,depth,leaf=False):
        def meta(s):return(s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY);rows=[]
        try:
            for i in range(depth+1):
                rows.append(meta(os.fstat(fd)))
                if i<depth:
                    child=os.open('d',os.O_RDONLY|os.O_DIRECTORY,dir_fd=fd);os.close(fd);fd=child
            if leaf:
                source=os.open('leaf',os.O_RDONLY,dir_fd=fd)
                try:rows.append((meta(os.fstat(source)),os.read(source,65536)))
                finally:os.close(source)
        finally:os.close(fd)
        return rows
    def close(self):
        fd=self.open(self.depth)
        try:
            for _ in range(self.depth):
                parent=os.open('..',os.O_RDONLY|os.O_DIRECTORY,dir_fd=fd);os.close(fd);fd=parent;os.rmdir('d',dir_fd=fd)
        finally:os.close(fd)
